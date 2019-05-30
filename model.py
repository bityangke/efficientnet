# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contains definitions for EfficientNet model.

[1] Mingxing Tan, Quoc V. Le
  EfficientNet: Rethinking Model Scaling for Convolutional Neural Networks.
  ICML'19, https://arxiv.org/abs/1905.11946
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import math
import numpy as np
import six
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import keras.backend as K
import keras.models as KM
import keras.layers as KL

from .layers import Swish, DropConnect
from .params import get_model_params


__all__ = ['EfficientNet', 'EfficientNetB0', 'EfficientNetB1', 'EfficientNetB2', 'EfficientNetB3']


def conv_kernel_initializer(shape, dtype=K.floatx(), partition_info=None):
    """Initialization for convolutional kernels.

    The main difference with tf.variance_scaling_initializer is that
    tf.variance_scaling_initializer uses a truncated normal with an uncorrected
    standard deviation, whereas here we use a normal distribution. Similarly,
    tf.contrib.layers.variance_scaling_initializer uses a truncated normal with
    a corrected standard deviation.

    Args:
      shape: shape of variable
      dtype: dtype of variable
      partition_info: unused

    Returns:
      an initialization for the variable
    """
    del partition_info
    kernel_height, kernel_width, _, out_filters = shape
    fan_out = int(kernel_height * kernel_width * out_filters)
    return tf.random_normal(
        shape, mean=0.0, stddev=np.sqrt(2.0 / fan_out), dtype=dtype)


def dense_kernel_initializer(shape, dtype=K.floatx(), partition_info=None):
    """Initialization for dense kernels.

    This initialization is equal to
      tf.variance_scaling_initializer(scale=1.0/3.0, mode='fan_out',
                                      distribution='uniform').
    It is written out explicitly here for clarity.

    Args:
      shape: shape of variable
      dtype: dtype of variable
      partition_info: unused

    Returns:
      an initialization for the variable
    """
    del partition_info
    init_range = 1.0 / np.sqrt(shape[1])
    return tf.random_uniform(shape, -init_range, init_range, dtype=dtype)


def round_filters(filters, global_params):
    """Round number of filters based on depth multiplier."""
    orig_f = filters
    multiplier = global_params.width_coefficient
    divisor = global_params.depth_divisor
    min_depth = global_params.min_depth
    if not multiplier:
        return filters

    filters *= multiplier
    min_depth = min_depth or divisor
    new_filters = max(min_depth, int(filters + divisor / 2) // divisor * divisor)
    # Make sure that round down does not go down by more than 10%.
    if new_filters < 0.9 * filters:
        new_filters += divisor
    print('round_filter input={} output={}'.format(orig_f, new_filters))
    return int(new_filters)


def round_repeats(repeats, global_params):
    """Round number of filters based on depth multiplier."""
    multiplier = global_params.depth_coefficient
    if not multiplier:
        return repeats
    return int(math.ceil(multiplier * repeats))


def SEBlock(block_args, global_params):
    num_reduced_filters = max(
        1, int(block_args.input_filters * block_args.se_ratio))
    filters = block_args.input_filters * block_args.expand_ratio
    if global_params.data_format == 'channels_first':
        channel_axis = 1
        spatial_dims = [2, 3]
    else:
        channel_axis = -1
        spatial_dims = [1, 2]

    def block(inputs):
        x = inputs
        x = KL.Lambda(lambda a: K.mean(a, axis=spatial_dims, keepdims=True))(x)
        x = KL.Conv2D(
            num_reduced_filters,
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=True
        )(x)
        x = Swish()(x)
        # Excite
        x = KL.Conv2D(
            filters,
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=True
        )(x)
        x = KL.Activation('sigmoid')(x)
        out = KL.Multiply()([x, inputs])
        return out

    return block


def MBConvBlock(block_args, global_params):
    batch_norm_momentum = global_params.batch_norm_momentum
    batch_norm_epsilon = global_params.batch_norm_epsilon

    if global_params.data_format == 'channels_first':
        channel_axis = 1
        spatial_dims = [2, 3]
    else:
        channel_axis = -1
        spatial_dims = [1, 2]

    has_se = (block_args.se_ratio is not None) and (
            block_args.se_ratio > 0) and (block_args.se_ratio <= 1)

    filters = block_args.input_filters * block_args.expand_ratio
    kernel_size = block_args.kernel_size

    def block(inputs):

        if block_args.expand_ratio != 1:
            x = KL.Conv2D(
                filters,
                kernel_size=[1, 1],
                strides=[1, 1],
                kernel_initializer=conv_kernel_initializer,
                padding='same',
                use_bias=False
            )(inputs)
            x = KL.BatchNormalization(
                axis=channel_axis,
                momentum=batch_norm_momentum,
                epsilon=batch_norm_epsilon
            )(x)
            x = Swish()(x)
        else:
            x = inputs

        x = KL.DepthwiseConv2D(
            [kernel_size, kernel_size],
            strides=block_args.strides,
            depthwise_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=False
        )(x)
        x = KL.BatchNormalization(
            axis=channel_axis,
            momentum=batch_norm_momentum,
            epsilon=batch_norm_epsilon
        )(x)
        x = Swish()(x)

        if has_se:
            x = SEBlock(block_args, global_params)(x)

        # output phase

        x = KL.Conv2D(
            block_args.output_filters,
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=False
        )(x)
        x = KL.BatchNormalization(
            axis=channel_axis,
            momentum=batch_norm_momentum,
            epsilon=batch_norm_epsilon
        )(x)

        if block_args.id_skip:
            if all(
                    s == 1 for s in block_args.strides
            ) and block_args.input_filters == block_args.output_filters:
                # only apply drop_connect if skip presents.
                if global_params.drop_connect_rate:
                    x = DropConnect(global_params.drop_connect_rate)(x)
                x = KL.Add()([x, inputs])
        return x

    return block


def EfficientNet(input_shape, block_args_list, global_params, include_top=True):
    batch_norm_momentum = global_params.batch_norm_momentum
    batch_norm_epsilon = global_params.batch_norm_epsilon
    if global_params.data_format == 'channels_first':
        channel_axis = 1
    else:
        channel_axis = -1

    # Stem part
    inputs = KL.Input(shape=input_shape)
    x = inputs
    x = KL.Conv2D(
        filters=round_filters(32, global_params),
        kernel_size=[3, 3],
        strides=[2, 2],
        kernel_initializer=conv_kernel_initializer,
        padding='same',
        use_bias=False
    )(x)
    x = KL.BatchNormalization(
        axis=channel_axis,
        momentum=batch_norm_momentum,
        epsilon=batch_norm_epsilon
    )(x)
    x = Swish()(x)

    # Blocks part
    for block_args in block_args_list:
        assert block_args.num_repeat > 0
        # Update block input and output filters based on depth multiplier.
        block_args = block_args._replace(
            input_filters=round_filters(block_args.input_filters, global_params),
            output_filters=round_filters(block_args.output_filters, global_params),
            num_repeat=round_repeats(block_args.num_repeat, global_params)
        )

        # The first block needs to take care of stride and filter size increase.
        x = MBConvBlock(block_args, global_params)(x)

        if block_args.num_repeat > 1:
            block_args = block_args._replace(input_filters=block_args.output_filters, strides=[1, 1])

        for _ in xrange(block_args.num_repeat - 1):
            x = MBConvBlock(block_args, global_params)(x)

    # Head part
    if include_top:

        x = KL.Conv2D(
            filters=round_filters(1280, global_params),
            kernel_size=[1, 1],
            strides=[1, 1],
            kernel_initializer=conv_kernel_initializer,
            padding='same',
            use_bias=False
        )(x)
        x = KL.BatchNormalization(
            axis=channel_axis,
            momentum=batch_norm_momentum,
            epsilon=batch_norm_epsilon
        )(x)
        x = Swish()(x)

        x = KL.GlobalAveragePooling2D(data_format=global_params.data_format)(x)
        if global_params.dropout_rate > 0:
            x = KL.Dropout(global_params.dropout_rate)(x)
        x = KL.Dense(global_params.num_classes, kernel_initializer=dense_kernel_initializer)(x)
        x = KL.Activation('softmax')(x)

    outputs = x
    model = KM.Model(inputs, outputs)

    return model


def _get_model_by_name(model_name, input_shape=None, include_top=True):
    block_agrs_list, global_params, default_input_shape = get_model_params(model_name, override_params=None)
    if not input_shape:
        input_shape = [default_input_shape, default_input_shape, 3]
    model = EfficientNet(input_shape, block_agrs_list, global_params)
    return model


def EfficientNetB0(include_top=True, input_shape=None):
    return _get_model_by_name('efficientnet-b0', include_top=include_top, input_shape=input_shape)


def EfficientNetB1(include_top=True, input_shape=None):
    return _get_model_by_name('efficientnet-b1', include_top=include_top, input_shape=input_shape)


def EfficientNetB2(include_top=True, input_shape=None):
    return _get_model_by_name('efficientnet-b2', include_top=include_top, input_shape=input_shape)


def EfficientNetB3(include_top=True, input_shape=None):
    return _get_model_by_name('efficientnet-b3', include_top=include_top, input_shape=input_shape)