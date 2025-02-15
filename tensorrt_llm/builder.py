# SPDX-FileCopyrightText: Copyright (c) 2022-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import json
import os
import time
from functools import wraps
from pathlib import Path
from typing import Optional, Union

import tensorrt as trt
from packaging import version

from ._utils import to_dict, to_json_file, trt_version
from .graph_rewriting import optimize
from .logger import logger
from .models import MODEL_MAP, PretrainedConfig, PretrainedModel
from .network import Network, net_guard
from .plugin import PluginConfig
from .plugin.plugin import ContextFMHAType
from .quantization import QuantMode
from .version import __version__


class _BuildingFlag:

    def __enter__(self):
        os.environ['IS_BUILDING'] = '1'

    def __exit__(self, type, value, tb):
        del os.environ['IS_BUILDING']


def _is_building(f):
    '''Use this to decorate functions which are called during engine building/refiting process,
    otherwise, the plugin registration will fail.
    '''

    @wraps(f)
    def decorated(*args, **kwargs):
        with _BuildingFlag():
            return f(*args, **kwargs)

    return decorated


class BuilderConfig(object):

    def __init__(self, **kwargs):
        # intentionally use **kwargs, user should never call this ctor directly,
        # use Builder.create_builder_config() instead
        pass

    def _init(self, trt_builder_config, **kwargs):
        self._trt_builder_config = trt_builder_config
        for key, value in kwargs.items():
            setattr(self, key, value)
        return self

    @property
    def trt_builder_config(self) -> trt.IBuilderConfig:
        return self._trt_builder_config


class Builder():

    _ALLOWED_PRECISIONS = ['float32', 'float16', 'bfloat16']

    def __init__(self):
        super().__init__()
        self._trt_builder = trt.Builder(logger.trt_logger)
        self.strongly_typed = False

    @property
    def trt_builder(self) -> trt.Builder:
        return self._trt_builder

    def create_network(self) -> Network:
        explicit_batch_flag = 0
        if "EXPLICIT_BATCH" in trt.NetworkDefinitionCreationFlag.__members__.keys(
        ):
            # Explicit batch flag will be deprecated in TRT 10
            explicit_batch_flag = 1 << int(
                trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)

        if version.parse(trt_version()) >= version.parse(
                "9.1.0") and self.strongly_typed:
            return Network()._init(
                self.trt_builder.create_network(
                    explicit_batch_flag
                    | (1 << int(
                        trt.NetworkDefinitionCreationFlag.STRONGLY_TYPED))))
        else:
            return Network()._init(
                self.trt_builder.create_network(explicit_batch_flag))

    def create_builder_config(self,
                              precision: str,
                              timing_cache: Union[str, Path,
                                                  trt.ITimingCache] = None,
                              tensor_parallel: int = 1,
                              use_refit: bool = False,
                              int8: bool = False,
                              strongly_typed: bool = False,
                              opt_level: Optional[int] = None,
                              **kwargs) -> BuilderConfig:
        ''' @brief Create a builder config with given precisions and timing cache
            @param precision: one of allowed precisions, defined in Builder._ALLOWED_PRECISIONS
            @param timing_cache: a timing cache object or a path to a timing cache file
            @param tensor_parallel: number of GPUs used for tensor parallel
            @param kwargs: any other arguments users would like to attach to the config object as attributes
            @param refit: set to accelerate multi-gpu building, build engine for 1 gpu and refit for the others
            @param int8: whether to build with int8 enabled or not. Can't be used together with refit option
            @return: A BuilderConfig object, return None if failed
        '''
        self.strongly_typed = strongly_typed

        quant_mode = kwargs.get("quant_mode", QuantMode(0))
        if not strongly_typed and precision not in self._ALLOWED_PRECISIONS:
            logger.error(
                f"precision should be one of {self._ALLOWED_PRECISIONS}")

        if use_refit and int8:
            # TRT folds weights into Myelin graph because network contains int8 tensor or Q/DQ nodes
            # These folded weights can not be refitted
            logger.error(f"can't use refit and int8 mode at the same time")

        config = self.trt_builder.create_builder_config()
        if not strongly_typed:
            fp8 = quant_mode.has_fp8_qdq() or quant_mode.has_fp8_kv_cache()

            if precision == 'float16':
                config.set_flag(trt.BuilderFlag.FP16)
                config.set_flag(trt.BuilderFlag.OBEY_PRECISION_CONSTRAINTS)
            elif precision == 'bfloat16':
                config.set_flag(trt.BuilderFlag.BF16)
                config.set_flag(trt.BuilderFlag.OBEY_PRECISION_CONSTRAINTS)
            if int8:
                config.set_flag(trt.BuilderFlag.INT8)

            if fp8:
                config.set_flag(trt.BuilderFlag.FP8)
                config.set_flag(trt.BuilderFlag.OBEY_PRECISION_CONSTRAINTS)

        config.set_preview_feature(trt.PreviewFeature.PROFILE_SHARING_0806,
                                   True)

        if use_refit:
            config.set_flag(trt.BuilderFlag.REFIT)

        if opt_level is not None:
            config.builder_optimization_level = opt_level

        # set timing cache
        cache = None
        if timing_cache is not None:
            # use given cache
            if isinstance(timing_cache, trt.ITimingCache):
                cache = timing_cache
            # read cache from file
            elif isinstance(timing_cache,
                            (str, Path)) and os.path.exists(timing_cache):
                with open(timing_cache, "rb") as f:
                    cache = config.create_timing_cache(f.read())
            else:
                logger.warning(
                    "Invalid timing cache, using freshly created one")
        if cache is None:
            cache = config.create_timing_cache(b"")
        # When user does not given any existing cache, internally always created one
        # so the cache should never None here
        assert cache is not None and isinstance(cache, trt.ITimingCache)
        config.set_timing_cache(cache, ignore_mismatch=False)

        return BuilderConfig()._init(config,
                                     precision=precision,
                                     tensor_parallel=tensor_parallel,
                                     use_refit=use_refit,
                                     int8=int8,
                                     **kwargs)

    def _add_optimization_profile(self, network: Network,
                                  builder_config: BuilderConfig):
        assert isinstance(builder_config, BuilderConfig)
        assert isinstance(network, Network)
        input_tensors = network._inputs
        num_profiles = len(list(input_tensors.items())[0][1].profiles)
        for i in range(num_profiles):
            logger.debug(f'Adding optimization profile {i+1}/{num_profiles}')
            profile = self.trt_builder.create_optimization_profile()
            for input_name in input_tensors.keys():
                shape_profile = input_tensors[input_name].profiles[i]
                profile.set_shape(input_name, shape_profile.min,
                                  shape_profile.opt, shape_profile.max)
                logger.debug(
                    f'{input_name}, min: {shape_profile.min}, opt: {shape_profile.opt}, max: {shape_profile.max}, dimension names: {shape_profile.dimension_names}'
                )
            builder_config.trt_builder_config.add_optimization_profile(profile)
        assert self._validate_named_dimensions(
            network, builder_config
        ), "Validation of the tensor dimension ranges failed, please check the dimension ranges, find the offensive tensor and dimension name in above the error log"

    def _validate_named_dimensions(self, network: Network,
                                   builder_config) -> bool:
        '''
            For each profile, validate that the named dimensions of different input tensors in this profile all have same range.
            TRT will validate the same condition, validate it earlier to make sure the modeling in TensorRT-LLM are correct and
            makes the error msg more user friendly.
        '''
        valid = True
        for profile_idx in range(
                builder_config.trt_builder_config.num_optimization_profiles):
            dimension_to_range = {}
            for input_name, input_tensor in network._inputs.items():
                # it's legal that a Tensor does not have dim_range?
                if len(input_tensor.profiles) != 0:
                    profile = input_tensor.profiles[profile_idx]
                    for dim_idx, dim_name in enumerate(profile.dimension_names):
                        if dim_name not in dimension_to_range:
                            dimension_to_range[dim_name] = []
                        min, opt, max = profile.min[dim_idx], profile.opt[
                            dim_idx], profile.max[dim_idx]
                        dimension_to_range[dim_name].append(
                            (input_name, (min, opt, max)))
            for dim, ranges in dimension_to_range.items():
                unique_ranges = set([r[1] for r in ranges])
                logger.debug(
                    f"Validating dimension:{dim}, ranges for this dim are:{unique_ranges}"
                )
                if len(unique_ranges) != 1:
                    logger.error(
                        f"Found illegal dimension setting for profile {profile_idx}, dimension name is: {dim}"
                    )
                    logger.error(
                        f"Offensive tensors which have this dimension are:\n" +
                        "\n".join([f"{r[1]} {dim} {r[0]}" for r in ranges]))
                    valid = False
        return valid

    @_is_building
    def refit_engine(self, network: Network, engine_buffer) -> trt.IHostMemory:
        '''
            @brief: Refit one TensorRT engine using weights from the network,
                user should guarantee that the engine is built with REFIT flag, and the network has the same structure with the engine.
            @param engine_buffer: A serialized TensorRT engine.
            @param network: Network object.
            @return: A serialized TRT engine if refit successfully, None otherwise
        '''
        assert isinstance(network, Network)
        logger.info(f'Refit TRT engine')
        runtime = trt.Runtime(logger.trt_logger)
        engine = runtime.deserialize_cuda_engine(engine_buffer)

        tik = time.time()

        # Refit engine
        refitter = trt.Refitter(engine, logger.trt_logger)
        if network.named_parameters is not None:
            for name, param in network.named_parameters:
                if param._get_weights(
                ) is None or not refitter.set_named_weights(
                        name, param._get_weights()):
                    logger.error(f'Failed to refit weight: {name}')
                    return None
        else:
            logger.error(
                f'Please set named parameters before building multiple engines.'
            )
            return None

        if not refitter.refit_cuda_engine():
            logger.error(f'Failed to refit engine.')
            return None

        tok = time.time()
        t = time.strftime('%H:%M:%S', time.gmtime(tok - tik))
        logger.info(f'Total time of refitting {engine.name}: {t}')
        serialized_engine = engine.serialize()
        return serialized_engine

    @_is_building
    def build_engine(self, network: Network,
                     builder_config: BuilderConfig) -> trt.IHostMemory:
        '''
            @brief: Build one TensorRT engine from the network.
            @param network: Network object.
            @param builder_config: BuilderConfig object.
            @return: A serialized TRT engine.
        '''
        assert isinstance(network, Network)
        builder_config.plugin_config = network.plugin_config
        self._add_optimization_profile(network, builder_config)
        engine = None
        logger.info(f'Build TensorRT engine {network.trt_network.name}')
        tik = time.time()

        # Rename weights
        if network.named_parameters is not None:
            for name, param in network.named_parameters:
                if param._get_weights(
                ) is None or not network.trt_network.set_weights_name(
                        param._get_weights(), name):
                    raise RuntimeError(f'Failed to set weight: {name}')

        # Build engine
        engine = self.trt_builder.build_serialized_network(
            network.trt_network, builder_config.trt_builder_config)
        if engine is None:
            logger.error('Engine building failed, please check the error log.')
            return None

        tok = time.time()
        t = time.strftime('%H:%M:%S', time.gmtime(tok - tik))
        logger.info(f'Total time of building {network.trt_network.name}: {t}')

        return engine

    @staticmethod
    def save_timing_cache(builder_config: BuilderConfig, out_path: str) -> bool:
        '''Serialize timing cache of given builder config to file specified by out_path
            return True if the cache is successfully serialized, False otherwise
        '''
        cache = builder_config.trt_builder_config.get_timing_cache()
        if cache is None:
            logger.warning(
                'No timing cache found in the given builder config, skip saving.'
            )
            return False
        with cache.serialize() as buffer:
            with open(out_path, "wb") as f:
                f.write(buffer)
                f.flush()
                os.fsync(f)
        logger.info(f'Timing cache serialized to {out_path}')
        return True

    @staticmethod
    def save_config(builder_config: BuilderConfig, config_path: str):
        config = {'builder_config': {}}
        for k in builder_config.__dict__.keys():
            if k != '_trt_builder_config' and k != 'plugin_config':
                config['builder_config'][k] = builder_config.__getattribute__(k)
        config['plugin_config'] = to_dict(builder_config.plugin_config)
        to_json_file(config, config_path)
        logger.info(f'Config saved to {config_path}.')


class BuildConfig:

    def __init__(self, max_input_len, max_output_len, max_batch_size,
                 max_beam_width, max_num_tokens,
                 max_prompt_embedding_table_size, gather_all_token_logits,
                 plugin_config):
        self.max_input_len = max_input_len
        self.max_output_len = max_output_len
        self.max_batch_size = max_batch_size
        self.max_beam_width = max_beam_width
        self.max_num_tokens = max_num_tokens
        self.max_prompt_embedding_table_size = max_prompt_embedding_table_size
        self.gather_all_token_logits = gather_all_token_logits
        self.plugin_config = plugin_config

    @classmethod
    def from_dict(cls, config):
        max_input_len = config.pop('max_input_len')
        max_output_len = config.pop('max_output_len')
        max_batch_size = config.pop('max_batch_size')
        max_beam_width = config.pop('max_beam_width')
        max_num_tokens = config.pop('max_num_tokens')
        max_prompt_embedding_table_size = config.pop(
            'max_prompt_embedding_table_size', 0)
        gather_all_token_logits = config.pop('gather_all_token_logits', False)

        plugin_config = PluginConfig()
        if 'plugin_config' not in config:
            return cls(
                max_input_len=max_input_len,
                max_output_len=max_output_len,
                max_batch_size=max_batch_size,
                max_beam_width=max_beam_width,
                max_num_tokens=max_num_tokens,
                max_prompt_embedding_table_size=max_prompt_embedding_table_size,
                gather_all_token_logits=gather_all_token_logits,
                plugin_config=plugin_config)

        config = config['plugin_config']
        gpt_attention_plugin = config.pop('gpt_attention_plugin', False)
        if gpt_attention_plugin:
            plugin_config.set_gpt_attention_plugin(dtype=gpt_attention_plugin)

        gemm_plugin = config.pop('gemm_plugin', False)
        if gemm_plugin:
            plugin_config.set_gemm_plugin(dtype=gemm_plugin)

        lookup_plugin = config.pop('lookup_plugin', False)
        if lookup_plugin:
            plugin_config.set_lookup_plugin(dtype=lookup_plugin)

        enable_context_fmha = config.pop('enable_context_fmha', False)
        enable_context_fmha_fp32_acc = config.pop(
            'enable_context_fmha_fp32_acc', False)
        assert not (enable_context_fmha and enable_context_fmha_fp32_acc)
        if enable_context_fmha:
            plugin_config.set_context_fmha(ContextFMHAType.enabled)
        if enable_context_fmha_fp32_acc:
            plugin_config.set_context_fmha(
                ContextFMHAType.enabled_with_fp32_acc)

        remove_input_padding = config.pop('remove_input_padding', False)
        if remove_input_padding:
            plugin_config.enable_remove_input_padding()

        paged_kv_cache = config.pop('paged_kv_cache', False)
        tokens_per_block = config.pop('tokens_per_block', 64)
        if paged_kv_cache:
            plugin_config.enable_paged_kv_cache(tokens_per_block)

        use_custom_all_reduce = config.pop('use_custom_all_reduce', False)
        plugin_config.use_custom_all_reduce = use_custom_all_reduce

        return cls(
            max_input_len=max_input_len,
            max_output_len=max_output_len,
            max_batch_size=max_batch_size,
            max_beam_width=max_beam_width,
            max_num_tokens=max_num_tokens,
            max_prompt_embedding_table_size=max_prompt_embedding_table_size,
            gather_all_token_logits=gather_all_token_logits,
            plugin_config=plugin_config)

    @classmethod
    def from_json_file(cls, config_file):
        with open(config_file) as f:
            config = json.load(f)
            return BuildConfig.from_dict(config)

    def to_dict(self):
        output = copy.deepcopy(self.__dict__)
        plugin_config = output.pop('plugin_config')
        plugin_config_dict = copy.deepcopy(plugin_config.__dict__)
        output['plugin_config'] = plugin_config_dict
        return output


def serialize_engine(engine, path):
    logger.info(f'Serializing engine to {path}...')
    tik = time.time()
    with open(path, 'wb') as f:
        f.write(bytearray(engine))
    tok = time.time()
    t = time.strftime('%H:%M:%S', time.gmtime(tok - tik))
    logger.info(f'Engine serialized. Total time: {t}')


class EngineConfig:

    def __init__(self, pretrained_config: PretrainedConfig,
                 build_config: BuildConfig, version: str):
        self.pretrained_config = pretrained_config
        self.build_config = build_config
        self.version = version

    @classmethod
    def from_json_file(cls, config_file):
        with open(config_file) as f:
            config = json.load(f)
            return cls(PretrainedConfig.from_dict(config['pretrained_config']),
                       BuildConfig.from_dict(config['build_config']),
                       config['version'])

    def to_dict(self):
        return {
            'version': self.version,
            'pretrained_config': self.pretrained_config.to_dict(),
            'build_config': self.build_config.to_dict(),
        }


class Engine:

    def __init__(self, config: EngineConfig, engine: trt.IHostMemory):
        self.config = config
        self.engine = engine

    def save(self, engine_dir: str):
        if self.config.pretrained_config.mapping.rank == 0:
            with open(os.path.join(engine_dir, 'config.json'),
                      "w",
                      encoding="utf-8") as f:
                json.dump(self.config.to_dict(), f, indent=4)
        serialize_engine(
            self.engine,
            os.path.join(
                engine_dir,
                f'rank{self.config.pretrained_config.mapping.rank}.engine'))

    @classmethod
    def from_dir(cls, engine_dir: str, rank: int = 0):
        with open(os.path.join(engine_dir, f'rank{rank}.engine'), 'rb') as f:
            engine_buffer = f.read()

        config = EngineConfig.from_json_file(
            os.path.join(engine_dir, 'config.json'))
        config.pretrained_config.set_rank(rank)

        return cls(config, engine_buffer)


def get_engine_version(engine_dir: str) -> Union[None, str]:
    engine_dir = Path(engine_dir)
    config_path = engine_dir / "config.json"
    with open(config_path, 'r') as f:
        config = json.load(f)

    if 'version' not in config:
        return None

    return config['version']


def build_shard_model(model: PretrainedModel,
                      build_config: BuildConfig) -> Engine:
    builder = Builder()
    network = builder.create_network()
    network._plugin_config = build_config.plugin_config

    use_weight_only = model.config.quant_mode.is_weight_only()
    per_group = model.config.quant_mode.has_per_group_scaling()
    use_smooth_quant = model.config.quant_mode.has_act_and_weight_quant()
    if use_weight_only:
        if per_group:
            network.plugin_config.set_weight_only_groupwise_quant_matmul_plugin(
                dtype='float16')
        else:
            network.plugin_config.set_weight_only_quant_matmul_plugin(
                dtype='float16')
    if use_smooth_quant:
        network.plugin_config.set_smooth_quant_gemm_plugin(dtype='float16')
        network.plugin_config.set_rmsnorm_quantization_plugin(dtype='float16')
        network.plugin_config.set_layernorm_quantization_plugin(dtype='float16')
        network.plugin_config.set_quantize_tensor_plugin()
        network.plugin_config.set_quantize_per_token_plugin()
    nccl_plugin = model.config.dtype if model.config.mapping.world_size > 1 else False
    if nccl_plugin:
        network.plugin_config.set_nccl_plugin(
            nccl_plugin, network.plugin_config.use_custom_all_reduce)

    with net_guard(network):
        # Prepare
        network.set_named_parameters(model.named_parameters())

        # Forward
        inputs = model.prepare_inputs(
            build_config.max_batch_size, build_config.max_input_len,
            build_config.max_output_len, True, build_config.max_beam_width,
            build_config.max_num_tokens,
            build_config.max_prompt_embedding_table_size)
        model(**inputs)

    optimize(network)

    builder_config = builder.create_builder_config(
        precision=model.config.dtype,
        int8=model.config.quant_mode.has_act_or_weight_quant())

    # Network -> Engine
    engine = builder.build_engine(network, builder_config)
    engine_config = EngineConfig(model.config, build_config, __version__)

    return Engine(engine_config, engine)


def build(build_config: Union[str, BuildConfig],
          rank: int = 0,
          ckpt_dir: str = None,
          model_config: Union[str, PretrainedConfig] = None,
          weights=None,
          model_cls=None) -> Engine:
    if ckpt_dir is not None:
        model_config = PretrainedConfig.from_json_file(
            os.path.join(ckpt_dir, 'config.json'))
    else:
        assert model_config is not None
        if isinstance(model_config, PretrainedConfig):
            model_config = model_config
        else:
            model_config = PretrainedConfig.from_json_file(model_config)

    if isinstance(build_config, str):
        build_config = BuildConfig.from_json_file(build_config)

    assert rank < model_config.mapping.world_size
    architecture = model_config.architecture

    if model_cls is None:
        if architecture not in MODEL_MAP:
            raise RuntimeError(
                f'Unsupported model architecture: {architecture}')
        model_cls = MODEL_MAP[architecture]

    if ckpt_dir is not None:
        model = model_cls.from_checkpoint(ckpt_dir, rank=rank)
    else:
        rank_config = copy.deepcopy(model_config)
        rank_config.set_rank(rank)
        model = model_cls.from_config(rank_config)
        if weights is not None:
            model.load(weights)
    return build_shard_model(model, build_config)
