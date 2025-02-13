# Copyright 2024 serket authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Convolutional layers."""

from __future__ import annotations

import abc
import functools as ft
import operator as op
from itertools import product
from typing import Sequence

import jax
import jax.numpy as jnp
import jax.random as jr
from typing_extensions import Annotated

from serket import TreeClass
from serket._src.nn.initialization import resolve_init
from serket._src.utils.convert import (
    calculate_transpose_padding,
    canonicalize,
    delayed_canonicalize_padding,
)
from serket._src.utils.lazy import maybe_lazy_call, maybe_lazy_init
from serket._src.utils.typing import (
    DilationType,
    DType,
    InitType,
    KernelSizeType,
    PaddingType,
    StridesType,
    Weight,
)
from serket._src.utils.validate import (
    validate_in_features_shape,
    validate_pos_int,
    validate_spatial_ndim,
)


def calculate_convolution_output_shape(
    shape: tuple[int, ...],
    kernel_size: tuple[int, ...],
    padding: tuple[tuple[int, int], ...],
    strides: tuple[int, ...],
):
    """Compute the shape of the output of a convolutional layer."""
    return tuple(
        (xi + (li + ri) - ki) // si + 1
        for xi, ki, si, (li, ri) in zip(shape, kernel_size, strides, padding)
    )


@ft.lru_cache(maxsize=None)
def generate_conv_dim_numbers(spatial_ndim) -> jax.lax.ConvDimensionNumbers:
    return jax.lax.ConvDimensionNumbers(*((tuple(range(spatial_ndim + 2)),) * 3))


def fft_conv_general_dilated(
    lhs: jax.Array,
    rhs: jax.Array,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    groups: int,
    dilation: Sequence[int],
) -> jax.Array:
    def pad(x: jax.Array, pad_width: tuple[tuple[int, int], ...]) -> jax.Array:
        return jnp.pad(x, [(max(lhs, 0), max(rhs, 0)) for (lhs, rhs) in (pad_width)])

    def intersperse(x: jax.Array, dilation: tuple[int, ...], axis: tuple[int, ...]):
        def along_axis(x: jax.Array, dilation: int, axis: int) -> jax.Array:
            shape = list(x.shape)
            shape[axis] = (dilation) * shape[axis] - (dilation - 1)
            z = jnp.zeros(shape)
            z = z.at[(slice(None),) * axis + (slice(None, None, (dilation)),)].set(x)
            return z

        for di, ai in zip(dilation, axis):
            x = along_axis(x, di, ai) if di > 1 else x
        return x

    def matmul(x, y, groups: int = 1):
        def ungrouped_matmul(x, y) -> jax.Array:
            alpha = "".join(map(str, range(max(x.ndim, y.ndim))))
            lhs = "a" + alpha[: x.ndim - 1]
            rhs = "b" + alpha[: y.ndim - 1]
            out = "ab" + lhs[2:]
            return jnp.einsum(f"{lhs},{rhs}->{out}", x, y)

        def grouped_matmul(x, y, groups) -> jax.Array:
            b, c, *s = x.shape  # batch, channels, spatial
            o, i, *k = y.shape  # out_channels, in_channels, kernel
            # groups, batch, channels, spatial
            x = x.reshape(groups, b, c // groups, *s)
            y = y.reshape(groups, o // groups, *(i, *k))
            z = jax.vmap(ungrouped_matmul, in_axes=(0, 0), out_axes=1)(x, y)
            return z.reshape(z.shape[0], z.shape[1] * z.shape[2], *z.shape[3:])

        return ungrouped_matmul(x, y) if groups == 1 else grouped_matmul(x, y, groups)

    spatial_ndim = lhs.ndim - 2  # spatial dimensions
    rhs = intersperse(rhs, dilation=dilation, axis=range(2, 2 + spatial_ndim))
    lhs = pad(lhs, ((0, 0), (0, 0), *padding))

    x_shape, w_shape = lhs.shape, rhs.shape

    if lhs.shape[-1] % 2 != 0:
        lhs = jnp.pad(lhs, tuple([(0, 0)] * (lhs.ndim - 1) + [(0, 1)]))

    kernel_pad = ((0, lhs.shape[i] - rhs.shape[i]) for i in range(2, spatial_ndim + 2))
    rhs = pad(rhs, ((0, 0), (0, 0), *kernel_pad))

    x_fft = jnp.fft.rfftn(lhs, axes=range(2, spatial_ndim + 2))
    w_fft = jnp.conjugate(jnp.fft.rfftn(rhs, axes=range(2, spatial_ndim + 2)))
    z_fft = matmul(x_fft, w_fft, groups)
    z = jnp.fft.irfftn(z_fft, axes=range(2, spatial_ndim + 2))

    start = (0,) * (spatial_ndim + 2)
    end = [z.shape[0], z.shape[1]]
    end += [max((x_shape[i] - w_shape[i] + 1), 0) for i in range(2, spatial_ndim + 2)]

    return (
        jax.lax.dynamic_slice(z, start, end)
        if all(s == 1 for s in strides)
        else jax.lax.slice(z, start, end, (1, 1, *strides))
    )


def fft_conv_nd(
    input: jax.Array,
    weight: Weight,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    dilation: Sequence[int],
    groups: int,
    mask: Weight | None = None,
) -> jax.Array:
    """Convolution function using fft.

    Note:
        Use ``jax.vmap`` to apply the convolution to a batch of input.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
         strides in each dimension.
        padding: padding of the input before convolution accepts tuple of integers
         for different padding in each dimension.
        dilation: dilation of the convolutional kernel accepts tuple of integers
            for different dilation in each dimension.
        groups: number of groups to use for grouped convolution.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set to ``None`` to not use a mask.
    """
    x = fft_conv_general_dilated(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        strides=strides,
        padding=padding,
        dilation=dilation,
        groups=groups,
    )

    return jnp.squeeze(x, 0) if bias is None else jnp.squeeze((x + bias), 0)


def fft_conv_nd_transpose(
    input: jax.Array,
    weight: Weight,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    dilation: Sequence[int],
    out_padding: int,
    mask: Weight | None = None,
) -> jax.Array:
    """Transposed convolution function using fft.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
         strides in each dimension.
        padding: padding of the input before convolution accepts tuple of integers
         for different padding in each dimension.
        dilation: dilation of the convolutional kernel accepts tuple of integers
            for different dilation in each dimension.
        out_padding: padding of the output after convolution.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set to ``None`` to not use a mask.
    """
    transposed_padding = calculate_transpose_padding(
        padding=padding,
        extra_padding=out_padding,
        kernel_size=weight.shape[2:],
        input_dilation=dilation,
    )
    x = fft_conv_general_dilated(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        strides=strides,
        padding=transposed_padding,
        dilation=dilation,
        groups=1,
    )

    return jnp.squeeze(x + bias, 0) if bias is not None else jnp.squeeze(x, 0)


def depthwise_fft_conv_nd(
    input: jax.Array,
    weight: Weight,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    mask: Weight | None = None,
) -> jax.Array:
    """Depthwise convolution function using fft.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
         strides in each dimension.
        padding: padding of the input before convolution accepts tuple of integers
         for different padding in each dimension.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set to ``None`` to not use a mask.
    """
    x = fft_conv_general_dilated(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        strides=strides,
        padding=padding,
        dilation=(1,) * (input.ndim - 1),
        groups=input.shape[0],  # in_features
    )

    return jnp.squeeze(x + bias, 0) if bias is not None else jnp.squeeze(x, 0)


def separable_fft_conv_nd(
    input: jax.Array,
    depthwise_weight: Weight,
    pointwise_weight: Weight,
    pointwise_bias: jax.Array | None,
    strides: Sequence[int],
    depthwise_padding: Sequence[tuple[int, int]],
    pointwise_padding: Sequence[tuple[int, int]],
    depthwise_mask: Weight | None = None,
    pointwise_mask: Weight | None = None,
) -> jax.Array:
    """Separable convolution function using fft.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        depthwise_weight: depthwise convolutional kernel.
        pointwise_weight: pointwise convolutional kernel.
        pointwise_bias: bias for the pointwise convolution.
        strides: stride of the convolution accepts tuple of integers for different
            strides in each dimension.
        depthwise_padding: padding of the input before depthwise convolution accepts
            Sequence of integers for different padding in each dimension.
        pointwise_padding: padding of the input before pointwise convolution accepts
            Sequence of integers for different padding in each dimension.
        depthwise_mask: a binary mask multiplied with the depthwise convolutional
            kernel. shape is ``(depth_multiplier * in_features, 1, *self.kernel_size)``.
            set to ``None`` to not use a mask.
        pointwise_mask: a binary mask multiplied with the pointwise convolutional
            kernel. shape is ``(out_features, depth_multiplier * in_features, 1, *self.kernel_size)``.
            set to ``None`` to not use a mask.
    """

    input = depthwise_fft_conv_nd(
        input=input,
        weight=depthwise_weight,
        bias=None,
        strides=strides,
        padding=depthwise_padding,
        mask=depthwise_mask,
    )

    return fft_conv_nd(
        input=input,
        weight=pointwise_weight,
        bias=pointwise_bias,
        strides=strides,
        padding=pointwise_padding,
        dilation=(1,) * (input.ndim - 1),
        groups=1,
        mask=pointwise_mask,
    )


def conv_nd(
    input: jax.Array,
    weight: Weight,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    dilation: Sequence[int],
    groups: int,
    mask: Weight | None = None,
) -> jax.Array:
    """Convolution function wrapping ``jax.lax.conv_general_dilated``.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
            strides in each dimension.
        padding: padding of the input before convolution accepts tuple of two integers
            for different padding in each dimension.
        dilation: dilation of the convolutional kernel accepts tuple of integers
            for different dilation in each dimension.
        groups: number of groups to use for grouped convolution.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set
    """
    x = jax.lax.conv_general_dilated(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        window_strides=strides,
        padding=padding,
        rhs_dilation=dilation,
        dimension_numbers=generate_conv_dim_numbers(input.ndim - 1),  # OIH...
        feature_group_count=groups,
    )

    return jnp.squeeze(x, 0) if bias is None else jnp.squeeze((x + bias), 0)


def conv_nd_transpose(
    input: jax.Array,
    weight: Weight,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    dilation: Sequence[int],
    out_padding: int,
    mask: Weight | None = None,
) -> jax.Array:
    """Transposed convolution function wrapping ``jax.lax.conv_general_dilated``.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
         strides in each dimension.
        padding: padding of the input before convolution accepts tuple of integers
         for different padding in each dimension.
        dilation: dilation of the convolutional kernel accepts tuple of integers
            for different dilation in each dimension.
        out_padding: padding of the output after convolution.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set to ``None`` to not use a mask.
    """
    transposed_padding = calculate_transpose_padding(
        padding=padding,
        extra_padding=out_padding,
        kernel_size=weight.shape[2:],
        input_dilation=dilation,
    )
    x = jax.lax.conv_transpose(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        strides=strides,
        padding=transposed_padding,
        rhs_dilation=dilation,
        dimension_numbers=generate_conv_dim_numbers(input.ndim - 1),
    )

    return jnp.squeeze(x + bias, 0) if bias is not None else jnp.squeeze(x, 0)


def separable_conv_nd(
    input: jax.Array,
    depthwise_weight: Weight,
    pointwise_weight: Weight,
    pointwise_bias: jax.Array | None,
    strides: Sequence[int],
    depthwise_padding: Sequence[tuple[int, int]],
    pointwise_padding: Sequence[tuple[int, int]],
    depthwise_mask: Weight | None = None,
    pointwise_mask: Weight | None = None,
) -> jax.Array:
    """Seprable convolution function wrapping ``jax.lax.conv_general_dilated``.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        depthwise_weight: depthwise convolutional kernel.
        pointwise_weight: pointwise convolutional kernel.
        pointwise_bias: bias for the pointwise convolution.
        strides: stride of the convolution accepts tuple of integers for different
            strides in each dimension.
        depthwise_padding: padding of the input before depthwise convolution accepts
            Sequence of integers for different padding in each dimension.
        pointwise_padding: padding of the input before pointwise convolution accepts
            Sequence of integers for different padding in each dimension.
        depthwise_mask: a binary mask multiplied with the depthwise convolutional
            kernel. shape is ``(depth_multiplier * in_features, 1, *self.kernel_size)``
            set to ``None`` to not use a mask.
        pointwise_mask: a binary mask multiplied with the pointwise convolutional
            kernel. shape is ``(out_features, depth_multiplier * in_features, *kernel_size)``
    """
    input = depthwise_conv_nd(
        input=input,
        weight=depthwise_weight,
        bias=None,
        strides=strides,
        padding=depthwise_padding,
        mask=depthwise_mask,
    )

    return conv_nd(
        input=input,
        weight=pointwise_weight,
        bias=pointwise_bias,
        strides=strides,
        padding=pointwise_padding,
        dilation=(1,) * (input.ndim - 1),
        groups=1,
        mask=pointwise_mask,
    )


def depthwise_conv_nd(
    input: jax.Array,
    weight: Weight,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    mask: Weight | None = None,
) -> jax.Array:
    """Depthwise convolution function wrapping ``jax.lax.conv_general_dilated``.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
         strides in each dimension.
        padding: padding of the input before convolution accepts tuple of integers
         for different padding in each dimension.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set to ``None`` to not use a mask.
    """
    x = jax.lax.conv_general_dilated(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        window_strides=strides,
        padding=padding,
        rhs_dilation=(1,) * (input.ndim - 1),
        dimension_numbers=generate_conv_dim_numbers(input.ndim - 1),
        feature_group_count=input.shape[0],  # in_features
    )

    return jnp.squeeze(x, 0) if bias is None else jnp.squeeze(x + bias, 0)


def spectral_conv_nd(
    input: Annotated[jax.Array, "I..."],
    weight: Weight,
    modes: Sequence[int],
) -> Annotated[jax.Array, "O..."]:
    """fourier neural operator convolution function.

    Args:
        input: input array. shape is ``(in_features, spatial size)``.
        weight: real and complex convolutional kernel. shape is ``(2 , 2 ** (dim-1), out_features, in_features, modes)``.
            where dim is the number of spatial dimensions on the
        modes: number of modes included in the fft representation of the input.
    """

    def generate_modes_slices(modes: Sequence[int]):
        *ms, ml = modes
        slices_ = [[slice(None, ml)]]
        slices_ += [[slice(None, mode), slice(-mode, None)] for mode in reversed(ms)]
        return [[slice(None)] + list(reversed(i)) for i in product(*slices_)]

    _, *si, sl = input.shape
    weight_r, weight_i = jnp.split(weight, 2, axis=0)
    weight = weight_r + 1j * weight_i
    _, o, *_ = weight.shape
    x_fft = jnp.fft.rfftn(input, s=(*si, sl))
    out = jnp.zeros([o, *si, sl // 2 + 1], dtype=input.dtype) + 0j
    for i, slice_i in enumerate(generate_modes_slices(modes)):
        matmul_out = jnp.einsum("i...,oi...->o...", x_fft[tuple(slice_i)], weight[i])
        out = out.at[tuple(slice_i)].set(matmul_out)
    return jnp.fft.irfftn(out, s=(*si, sl))


def local_conv_nd(
    input: jax.Array,
    weight: jax.Array,
    bias: jax.Array | None,
    strides: Sequence[int],
    padding: Sequence[tuple[int, int]],
    dilation: Sequence[int],
    kernel_size: Sequence[int],
    mask: Weight | None = None,
) -> jax.Array:
    """Local convolution function wrapping ``jax.lax.conv_general_dilated_local``.

    Args:
        input: input array. shape is ``(in_features, spatial)``.
        weight: convolutional kernel. shape is ``(out_features, in_features, kernel)``.
        bias: bias. shape is ``(out_features, spatial)``. set to ``None`` to not use a bias.
        strides: stride of the convolution accepts tuple of integers for different
         strides in each dimension.
        padding: padding of the input before convolution accepts tuple of integers
         for different padding in each dimension.
        dilation: dilation of the convolution accepts tuple of integers for different
            dilation in each dimension.
        kernel_size: size of the convolutional kernel accepts tuple of integers for
            different kernel sizes in each dimension.
        mask: a binary mask multiplied with the convolutional kernel. shape is
            ``(out_features, in_features, kernel)``. set to ``None`` to not use a mask.
    """
    x = jax.lax.conv_general_dilated_local(
        lhs=jnp.expand_dims(input, 0),
        rhs=weight if mask is None else weight * mask,
        window_strides=strides,
        padding=padding,
        filter_shape=kernel_size,
        rhs_dilation=dilation,
        dimension_numbers=generate_conv_dim_numbers(input.ndim - 1),
    )

    return jnp.squeeze(x + bias, 0) if bias is not None else jnp.squeeze(x, 0)


def is_lazy_call(instance, *_1, **_2) -> bool:
    return getattr(instance, "in_features", False) is None


def is_lazy_init(_1, in_features, *_2, **_3) -> bool:
    return in_features is None


def infer_in_features(_1, x, *_2, **_3) -> int:
    return x.shape[0]


updates = dict(in_features=infer_in_features)


class ConvND(TreeClass):
    @ft.partial(maybe_lazy_init, is_lazy=is_lazy_init)
    def __init__(
        self,
        in_features: int | None,
        out_features: int,
        kernel_size: KernelSizeType,
        *,
        key: jax.Array,
        strides: StridesType = 1,
        padding: PaddingType = "same",
        dilation: DilationType = 1,
        weight_init: InitType = "glorot_uniform",
        bias_init: InitType = "zeros",
        groups: int = 1,
        dtype: DType = jnp.float32,
    ):
        self.in_features = validate_pos_int(in_features)
        self.out_features = validate_pos_int(out_features)
        self.kernel_size = canonicalize(kernel_size, self.spatial_ndim, "kernel_size")
        self.strides = canonicalize(strides, self.spatial_ndim, "strides")
        self.padding = padding
        self.dilation = canonicalize(dilation, self.spatial_ndim, "dilation")
        self.weight_init = weight_init
        self.bias_init = bias_init
        self.groups = validate_pos_int(groups)

        if self.out_features % self.groups != 0:
            raise ValueError(f"{(out_features % groups == 0)=}")

        weight_shape = (out_features, in_features // groups, *self.kernel_size)
        self.weight = resolve_init(self.weight_init)(key, weight_shape, dtype)

        bias_shape = (out_features, *(1,) * self.spatial_ndim)
        self.bias = resolve_init(self.bias_init)(key, bias_shape, dtype)

    @ft.partial(maybe_lazy_call, is_lazy=is_lazy_call, updates=updates)
    @ft.partial(validate_spatial_ndim, argnum=0)
    @ft.partial(validate_in_features_shape, axis=0)
    def __call__(self, input: jax.Array, mask: Weight | None = None) -> jax.Array:
        """Apply the layer.

        Args:
            input: input array. shape is ``(in_features, spatial size)``. spatial size
                is length for 1D convolution, height, width for 2D convolution and
                height, width, depth for 3D convolution.
            mask: a binary mask multiplied with the convolutional kernel. shape is
                ``(out_features, in_features // groups, kernel size)``. set to ``None``
                to not use a mask.
        """
        padding = delayed_canonicalize_padding(
            in_dim=input.shape[1:],
            padding=self.padding,
            kernel_size=self.kernel_size,
            strides=self.strides,
        )

        return self.conv_op(
            input=input,
            weight=self.weight,
            bias=self.bias,
            strides=self.strides,
            padding=padding,
            dilation=self.dilation,
            groups=self.groups,
            mask=mask,
        )

    spatial_ndim = property(abc.abstractmethod(lambda _: ...))
    conv_op = staticmethod(abc.abstractmethod(lambda _: ...))


class Conv1D(ConvND):
    """1D Convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.Conv1D(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5))
        >>> print(layer(input).shape)
        (2, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5)

    Note:
        :class:`.Conv1D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv1D(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(conv_nd)


class Conv2D(ConvND):
    """2D Convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        dilation: dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.Conv2D(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5)

    Note:
        :class:`.Conv2D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv2D(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(conv_nd)


class Conv3D(ConvND):
    """3D Convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        dilation: dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.Conv3D(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5, 5)

    Note:
        :class:`.Conv3D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv3D(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(conv_nd)


class FFTConv1D(ConvND):
    """1D Convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.FFTConv1D(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5))
        >>> print(layer(input).shape)
        (2, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5)

    Note:
        :class:`.FFTConv1D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.FFTConv1D(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(fft_conv_nd)


class FFTConv2D(ConvND):
    """2D FFT Convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.FFTConv2D(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5)

    Note:
        :class:`.FFTConv2D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.FFTConv2D(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(fft_conv_nd)


class FFTConv3D(ConvND):
    """3D FFT Convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.FFTConv3D(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5, 5)

    Note:
        :class:`.FFTConv3D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.FFTConv3D(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(fft_conv_nd)


class ConvNDTranspose(TreeClass):
    @ft.partial(maybe_lazy_init, is_lazy=is_lazy_init)
    def __init__(
        self,
        in_features: int | None,
        out_features: int,
        kernel_size: KernelSizeType,
        *,
        key: jax.Array,
        strides: StridesType = 1,
        padding: PaddingType = "same",
        out_padding: int = 0,
        dilation: DilationType = 1,
        weight_init: InitType = "glorot_uniform",
        bias_init: InitType = "zeros",
        groups: int = 1,
        dtype: DType = jnp.float32,
    ):
        self.in_features = validate_pos_int(in_features)
        self.out_features = validate_pos_int(out_features)
        self.kernel_size = canonicalize(kernel_size, self.spatial_ndim, "kernel_size")
        self.strides = canonicalize(strides, self.spatial_ndim, "strides")
        self.padding = padding  # delayed canonicalization
        self.out_padding = canonicalize(out_padding, self.spatial_ndim, "out_padding")
        self.dilation = canonicalize(dilation, self.spatial_ndim, "dilation")
        self.weight_init = weight_init
        self.bias_init = bias_init
        self.groups = validate_pos_int(groups)

        if self.out_features % self.groups != 0:
            raise ValueError(f"{(self.out_features % self.groups ==0)=}")

        in_features = validate_pos_int(self.in_features)
        weight_shape = (out_features, in_features // groups, *self.kernel_size)
        self.weight = resolve_init(self.weight_init)(key, weight_shape, dtype)

        bias_shape = (out_features, *(1,) * self.spatial_ndim)
        self.bias = resolve_init(self.bias_init)(key, bias_shape, dtype)

    @ft.partial(maybe_lazy_call, is_lazy=is_lazy_call, updates=updates)
    @ft.partial(validate_spatial_ndim, argnum=0)
    @ft.partial(validate_in_features_shape, axis=0)
    def __call__(self, input: jax.Array, mask: Weight | None = None) -> jax.Array:
        """Apply the layer.

        Args:
            input: input array. shape is ``(in_features, spatial size)``. spatial size
                is length for 1D convolution, height, width for 2D convolution and
                height, width, depth for 3D convolution.
            mask: a binary mask multiplied with the convolutional kernel. shape is
                ``(out_features, in_features // groups, kernel size)``. set to ``None``
                to not use a mask.
        """

        padding = delayed_canonicalize_padding(
            in_dim=input.shape[1:],
            padding=self.padding,
            kernel_size=self.kernel_size,
            strides=self.strides,
        )

        return self.conv_op(
            input=input,
            weight=self.weight,
            bias=self.bias,
            strides=self.strides,
            padding=padding,
            dilation=self.dilation,
            out_padding=self.out_padding,
            mask=mask,
        )

    spatial_ndim = property(abc.abstractmethod(lambda _: ...))
    conv_op = staticmethod(abc.abstractmethod(lambda _: ...))


class Conv1DTranspose(ConvNDTranspose):
    """1D Convolution transpose layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        out_padding: padding of the output after convolution. accepts:

            - Single integer for same padding in all dimensions.

        dilation: dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.Conv1DTranspose(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5))
        >>> print(layer(input).shape)
        (2, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5)

    Note:
        :class:`.Conv1DTranspose` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv1DTranspose(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(conv_nd_transpose)


class Conv2DTranspose(ConvNDTranspose):
    """2D Convolution transpose layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        out_padding: padding of the output after convolution. accepts:

            - Single integer for same padding in all dimensions.

        dilation: dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> key = jr.key(0)
        >>> layer = sk.nn.Conv2DTranspose(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5)

    Note:
        :class:`.Conv2DTranspose` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv2DTranspose(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(conv_nd_transpose)


class Conv3DTranspose(ConvNDTranspose):
    """3D Convolution transpose layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        out_padding: padding of the output after convolution. accepts:

            - Single integer for same padding in all dimensions.

        dilation: dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.Conv3DTranspose(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5, 5)

    Note:
        :class:`.Conv3DTranspose` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv3DTranspose(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(conv_nd_transpose)


class FFTConv1DTranspose(ConvNDTranspose):
    """1D FFT Convolution transpose layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        out_padding: Padding of the output after convolution. accepts:

            - Single integer for same padding in all dimensions.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.FFTConv1DTranspose(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5))
        >>> print(layer(input).shape)
        (2, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5)

    Note:
        :class:`.FFTConv1DTranspose` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.FFTConv1DTranspose(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(fft_conv_nd_transpose)


class FFTConv2DTranspose(ConvNDTranspose):
    """2D FFT Convolution transpose layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        out_padding: Padding of the output after convolution. accepts:

            - Single integer for same padding in all dimensions.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.FFTConv2DTranspose(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5)

    Note:
        :class:`.FFTConv2DTranspose` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.FFTConv2DTranspose(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(fft_conv_nd_transpose)


class FFTConv3DTranspose(ConvNDTranspose):
    """3D FFT Convolution transpose layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        out_padding: Padding of the output after convolution. accepts:

            - Single integer for same padding in all dimensions.

        dilation: Dilation of the convolutional kernel accepts:

            - Single integer for same dilation in all dimensions.
            - Sequence of integers for different dilation in each dimension.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        groups: number of groups to use for grouped convolution.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> layer = sk.nn.FFTConv3DTranspose(1, 2, 3, key=key)
        >>> # single sample
        >>> input = jnp.ones((1, 5, 5, 5))
        >>> print(layer(input).shape)
        (2, 5, 5, 5)
        >>> # batch of samples
        >>> input = jnp.ones((2, 1, 5, 5, 5))
        >>> print(jax.vmap(layer)(input).shape)
        (2, 2, 5, 5, 5)

    Note:
        :class:`.FFTConv3DTranspose` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.FFTConv3DTranspose(None, 12, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(fft_conv_nd_transpose)


class DepthwiseConvND(TreeClass):
    @ft.partial(maybe_lazy_init, is_lazy=is_lazy_init)
    def __init__(
        self,
        in_features: int | None,
        kernel_size: KernelSizeType,
        *,
        key: jax.Array,
        depth_multiplier: int = 1,
        strides: int = 1,
        padding: PaddingType = "same",
        weight_init: InitType = "glorot_uniform",
        bias_init: InitType = "zeros",
        dtype: DType = jnp.float32,
    ):
        self.in_features = validate_pos_int(in_features)
        self.kernel_size = canonicalize(kernel_size, self.spatial_ndim, "kernel_size")
        self.depth_multiplier = validate_pos_int(depth_multiplier)
        self.strides = canonicalize(strides, self.spatial_ndim, "strides")
        self.padding = padding  # delayed canonicalization
        self.weight_init = weight_init
        self.bias_init = bias_init

        weight_shape = (depth_multiplier * in_features, 1, *self.kernel_size)  # OIH...
        self.weight = resolve_init(self.weight_init)(key, weight_shape, dtype)

        bias_shape = (depth_multiplier * in_features, *(1,) * self.spatial_ndim)
        self.bias = resolve_init(self.bias_init)(key, bias_shape, dtype)

    @ft.partial(maybe_lazy_call, is_lazy=is_lazy_call, updates=updates)
    @ft.partial(validate_spatial_ndim, argnum=0)
    @ft.partial(validate_in_features_shape, axis=0)
    def __call__(self, input: jax.Array, mask: Weight | None = None) -> jax.Array:
        """Apply the layer.

        Args:
            input: input array. shape is ``(in_features, spatial size)``. spatial size
                is length for 1D convolution, height, width for 2D convolution and
                height, width, depth for 3D convolution.
            mask: a binary mask multiplied with the convolutional kernel. shape is
                ``(depth_multiplier * in_features, 1, *self.kernel_size)``. set to ``None``
                to not use a mask.
        """
        padding = delayed_canonicalize_padding(
            in_dim=input.shape[1:],
            padding=self.padding,
            kernel_size=self.kernel_size,
            strides=self.strides,
        )

        return self.conv_op(
            input=input,
            weight=self.weight,
            bias=self.bias,
            strides=self.strides,
            padding=padding,
            mask=mask,
        )

    spatial_ndim = property(abc.abstractmethod(lambda _: ...))
    conv_op = staticmethod(abc.abstractmethod(lambda _: ...))


class DepthwiseConv1D(DepthwiseConvND):
    """1D Depthwise convolution layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.DepthwiseConv1D(3, 3, depth_multiplier=2, strides=2, key=key)
        >>> l1(jnp.ones((3, 32))).shape
        (6, 16)

    Note:
        :class:`.DepthwiseConv1D` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.DepthwiseConv1D(None, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(depthwise_conv_nd)


class DepthwiseConv2D(DepthwiseConvND):
    """2D Depthwise convolution layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.DepthwiseConv2D(3, 3, depth_multiplier=2, strides=2, key=key)
        >>> l1(jnp.ones((3, 32, 32))).shape
        (6, 16, 16)

    Note:
        :class:`.DepthwiseConv2D` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.DepthwiseConv2D(None, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(depthwise_conv_nd)


class DepthwiseConv3D(DepthwiseConvND):
    """3D Depthwise convolution layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.

        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        padding: adding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.DepthwiseConv3D(3, 3, depth_multiplier=2, strides=2, key=key)
        >>> l1(jnp.ones((3, 32, 32, 32))).shape
        (6, 16, 16, 16)

    Note:
        :class:`.DepthwiseConv3D` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.DepthwiseConv3D(None, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(depthwise_conv_nd)


class DepthwiseFFTConv1D(DepthwiseConvND):
    """1D Depthwise FFT convolution layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.DepthwiseFFTConv1D(3, 3, depth_multiplier=2, strides=2, key=key)
        >>> l1(jnp.ones((3, 32))).shape
        (6, 16)

    Note:
        :class:`.DepthwiseFFTConv1D` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.DepthwiseFFTConv1D(None, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(depthwise_fft_conv_nd)


class DepthwiseFFTConv2D(DepthwiseConvND):
    """2D Depthwise convolution layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        kernel_size: Size of the convolutional kernel. accepts:

           - single integer for same kernel size in all dimnsions.
           - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.DepthwiseFFTConv2D(3, 3, depth_multiplier=2, strides=2, key=key)
        >>> l1(jnp.ones((3, 32, 32))).shape
        (6, 16, 16)

    Note:
        :class:`.DepthwiseFFTConv2D` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.DepthwiseFFTConv2D(None, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(depthwise_fft_conv_nd)


class DepthwiseFFTConv3D(DepthwiseConvND):
    """3D Depthwise FFT convolution layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        kernel_size: Size of the convolutional kernel. accepts:

           - single integer for same kernel size in all dimnsions.
           - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.DepthwiseFFTConv3D(3, 3, depth_multiplier=2, strides=2, key=key)
        >>> l1(jnp.ones((3, 32, 32, 32))).shape
        (6, 16, 16, 16)

    Note:
        :class:`.DepthwiseFFTConv3D` supports lazy initialization, meaning that the
        weights and biases are not initialized until the first call to the layer.
        This is useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.DepthwiseFFTConv3D(None, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(depthwise_fft_conv_nd)


class SeparableConvND(TreeClass):
    @ft.partial(maybe_lazy_init, is_lazy=is_lazy_init)
    def __init__(
        self,
        in_features: int | None,
        out_features: int,
        kernel_size: KernelSizeType,
        *,
        key: jax.Array,
        depth_multiplier: int = 1,
        strides: StridesType = 1,
        padding: PaddingType = "same",
        depthwise_weight_init: InitType = "glorot_uniform",
        pointwise_weight_init: InitType = "glorot_uniform",
        pointwise_bias_init: InitType = "zeros",
        dtype: DType = jnp.float32,
    ):
        self.in_features = validate_pos_int(in_features)
        self.kernel_size = canonicalize(kernel_size, self.spatial_ndim, "kernel_size")
        self.depth_multiplier = validate_pos_int(depth_multiplier)
        self.strides = canonicalize(strides, self.spatial_ndim, "strides")
        self.padding = padding  # delayed canonicalization
        self.depthwise_weight_init = depthwise_weight_init
        self.pointwise_weight_init = pointwise_weight_init
        self.pointwise_bias_init = pointwise_bias_init

        # depthwise initialization
        weight_shape = (depth_multiplier * in_features, 1, *self.kernel_size)
        args = (key, weight_shape, dtype)
        self.depthwise_weight = resolve_init(self.depthwise_weight_init)(*args)
        # pointwise initialization
        kernel_size = canonicalize(1, self.spatial_ndim)
        weight_shape = (out_features, depth_multiplier * in_features, *kernel_size)
        args = (key, weight_shape, dtype)
        self.pointwise_weight = resolve_init(self.pointwise_bias_init)(*args)
        bias_shape = (out_features, *(1,) * self.spatial_ndim)
        args = (key, bias_shape, dtype)
        self.pointwise_bias = resolve_init(self.pointwise_bias_init)(*args)

    @ft.partial(maybe_lazy_call, is_lazy=is_lazy_call, updates=updates)
    @ft.partial(validate_spatial_ndim, argnum=0)
    @ft.partial(validate_in_features_shape, axis=0)
    def __call__(
        self,
        input: jax.Array,
        depthwise_mask: Weight | None = None,
        pointwise_mask: Weight | None = None,
    ) -> jax.Array:
        """Apply the layer.

        Args:
            input: input array. shape is ``(in_features, spatial size)``. spatial size
                is length for 1D convolution, height, width for 2D convolution and
                height, width, depth for 3D convolution.
            depthwise_mask: a binary mask multiplied with the depthwise convolutional
                kernel. shape is ``(depth_multiplier * in_features, 1, *self.kernel_size)``.
                set to ``None`` to not use a mask.
            pointwise_mask: a binary mask multiplied with the pointwise convolutional
                kernel. shape is ``(out_features, depth_multiplier * in_features, 1, *self.kernel_size)``.
                set to ``None`` to not use a mask.
        """
        depthwise_padding = delayed_canonicalize_padding(
            in_dim=input.shape[1:],
            padding=self.padding,
            kernel_size=self.kernel_size,
            strides=self.strides,
        )
        pointwise_padding = delayed_canonicalize_padding(
            in_dim=input.shape[1:],
            padding=self.padding,
            kernel_size=canonicalize(1, self.spatial_ndim),
            strides=self.strides,
        )

        return self.conv_op(
            input=input,
            depthwise_weight=self.depthwise_weight,
            pointwise_weight=self.pointwise_weight,
            pointwise_bias=self.pointwise_bias,
            strides=self.strides,
            depthwise_padding=depthwise_padding,
            pointwise_padding=pointwise_padding,
            depthwise_mask=depthwise_mask,
            pointwise_mask=pointwise_mask,
        )

    spatial_ndim = property(abc.abstractmethod(lambda _: ...))
    conv_op = staticmethod(abc.abstractmethod(lambda _: ...))


class SeparableConv1D(SeparableConvND):
    """1D Separable convolution layer.

    Separable convolution is a depthwise convolution followed by a pointwise
    convolution. The objective is to reduce the number of parameters in the
    convolutional layer. For example, for I input features and O output features,
    and a kernel size = Ki, then standard convolution has I * O * K0 ... * Kn + O
    parameters, whereas separable convolution has I * K0 ... * Kn + I * O + O
    parameters.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SeparableConv1D(3, 3, 3, depth_multiplier=2, key=key)
        >>> l1(jnp.ones((3, 32))).shape
        (3, 32)

    Note:
        :class:`.SeparableConv1D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SeparableConv1D(None, 2, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(separable_conv_nd)


class SeparableConv2D(SeparableConvND):
    """2D Separable convolution layer.

    Separable convolution is a depthwise convolution followed by a pointwise
    convolution. The objective is to reduce the number of parameters in the
    convolutional layer. For example, for I input features and O output features,
    and a kernel size = Ki, then standard convolution has I * O * K0 ... * Kn + O
    parameters, whereas separable convolution has I * K0 ... * Kn + I * O + O
    parameters.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SeparableConv2D(3, 3, 3, depth_multiplier=2, key=key)
        >>> l1(jnp.ones((3, 32, 32))).shape
        (3, 32, 32)

    Note:
        :class:`.SeparableConv2D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SeparableConv2D(None, 2, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(separable_conv_nd)


class SeparableConv3D(SeparableConvND):
    """3D Separable convolution layer.

    Separable convolution is a depthwise convolution followed by a pointwise
    convolution. The objective is to reduce the number of parameters in the
    convolutional layer. For example, for I input features and O output features,
    and a kernel size = Ki, then standard convolution has I * O * K0 ... * Kn + O
    parameters, whereas separable convolution has I * K0 ... * Kn + I * O + O
    parameters.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SeparableConv3D(3, 3, 3, depth_multiplier=2, key=key)
        >>> l1(jnp.ones((3, 32, 32, 32))).shape
        (3, 32, 32, 32)

    Note:
        :class:`.SeparableConv3D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SeparableConv3D(None, 2, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(separable_conv_nd)


class SeparableFFTConv1D(SeparableConvND):
    """1D Separable FFT convolution layer.

    Separable convolution is a depthwise convolution followed by a pointwise
    convolution. The objective is to reduce the number of parameters in the
    convolutional layer. For example, for I input features and O output features,
    and a kernel size = Ki, then standard convolution has I * O * K0 ... * Kn + O
    parameters, whereas separable convolution has I * K0 ... * Kn + I * O + O
    parameters.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

           - single integer for same kernel size in all dimnsions.
           - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SeparableFFTConv1D(3, 3, 3, depth_multiplier=2, key=key)
        >>> l1(jnp.ones((3, 32))).shape
        (3, 32)

    Note:
        :class:`.SeparableFFTConv1D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SeparableFFTConv1D(None, 2, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(separable_fft_conv_nd)


class SeparableFFTConv2D(SeparableConvND):
    """2D Separable FFT convolution layer.

    Separable convolution is a depthwise convolution followed by a pointwise
    convolution. The objective is to reduce the number of parameters in the
    convolutional layer. For example, for I input features and O output features,
    and a kernel size = Ki, then standard convolution has I * O * K0 ... * Kn + O
    parameters, whereas separable convolution has I * K0 ... * Kn + I * O + O
    parameters.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

           - single integer for same kernel size in all dimnsions.
           - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SeparableFFTConv2D(3, 3, 3, depth_multiplier=2, key=key)
        >>> l1(jnp.ones((3, 32, 32))).shape
        (3, 32, 32)

    Note:
        :class:`.SeparableFFTConv2D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SeparableFFTConv2D(None, 2, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(separable_fft_conv_nd)


class SeparableFFTConv3D(SeparableConvND):
    """3D Separable FFT convolution layer.

    Separable convolution is a depthwise convolution followed by a pointwise
    convolution. The objective is to reduce the number of parameters in the
    convolutional layer. For example, for I input features and O output features,
    and a kernel size = Ki, then standard convolution has I * O * K0 ... * Kn + O
    parameters, whereas separable convolution has I * K0 ... * Kn + I * O + O
    parameters.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

           - single integer for same kernel size in all dimnsions.
           - Sequence of integers for different kernel sizes in each dimension.

        depth_multiplier: multiplier for the number of output channels. for example
            if the input has 32 channels and the depth multiplier is 2 then the
            output will have 64 channels.
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SeparableFFTConv3D(3, 3, 3, depth_multiplier=2, key=key)
        >>> l1(jnp.ones((3, 32, 32, 32))).shape
        (3, 32, 32, 32)

    Note:
        :class:`.SeparableFFTConv3D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SeparableFFTConv3D(None, 2, 3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    References:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(separable_fft_conv_nd)


class SpectralConvND(TreeClass):
    @ft.partial(maybe_lazy_init, is_lazy=is_lazy_init)
    def __init__(
        self,
        in_features: int,
        out_features: int,
        *,
        modes: int | tuple[int, ...],
        key: jax.Array,
        dtype: DType = jnp.float32,
    ):
        self.in_features = validate_pos_int(in_features)
        self.out_features = validate_pos_int(out_features)
        self.modes: tuple[int, ...] = canonicalize(modes, self.spatial_ndim, "modes")
        weight_shape = (1, out_features, in_features, *self.modes)
        scale = 1 / (in_features * out_features)
        k1, k2 = jr.split(key)
        self.weight_r = scale * jr.normal(k1, weight_shape).astype(dtype)
        self.weight_i = scale * jr.normal(k2, weight_shape).astype(dtype)

    @ft.partial(maybe_lazy_call, is_lazy=is_lazy_call, updates=updates)
    @ft.partial(validate_spatial_ndim, argnum=0)
    @ft.partial(validate_in_features_shape, axis=0)
    def __call__(self, input: jax.Array) -> jax.Array:
        return self.conv_op(
            input=input,
            weight=jnp.concatenate([self.weight_r, self.weight_i], axis=0),
            modes=self.modes,
        )

    spatial_ndim = property(abc.abstractmethod(lambda _: ...))
    conv_op = staticmethod(abc.abstractmethod(lambda _: ...))


class SpectralConv1D(SpectralConvND):
    """1D Spectral convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.

        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.

        modes: Number of modes to use in the spectral convolution.

        key: key to use for initializing the weights.
        dtype: dtype of the weights. defaults to ``float32``


    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SpectralConv1D(3, 3, modes=1, key=key)
        >>> l1(jnp.ones((3, 32))).shape
        (3, 32)

    Note:
        :class:`.SpectralConv1D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SpectralConv1D(None, 2, modes=3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://zongyi-li.github.io/blog/2020/fourier-pde/
        - https://arxiv.org/abs/2010.08895
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(spectral_conv_nd)


class SpectralConv2D(SpectralConvND):
    """2D Spectral convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.

        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.

        modes: Number of modes to use in the spectral convolution. accepts two
            integer tuple for different modes in each dimension. or a single
            integer for the same number of modes in each dimension.

        key: key to use for initializing the weights.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SpectralConv2D(3, 3, modes=(1, 2), key=key)
        >>> l1(jnp.ones((3, 32 ,32))).shape
        (3, 32, 32)

    Note:
        :class:`.SpectralConv2D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SpectralConv2D(None, 2, modes=3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://zongyi-li.github.io/blog/2020/fourier-pde/
        - https://arxiv.org/abs/2010.08895
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(spectral_conv_nd)


class SpectralConv3D(SpectralConvND):
    """3D Spectral convolutional layer.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.

        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.

        modes: Number of modes to use in the spectral convolution. accepts three
            integer tuple for different modes in each dimension. or a single
            integer for the same number of modes in each dimension.

        key: key to use for initializing the weights.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.SpectralConv3D(3, 3, modes=(1, 2, 2), key=key)
        >>> l1(jnp.ones((3, 32, 32, 32))).shape
        (3, 32, 32, 32)

    Note:
        :class:`.SpectralConv3D` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.SpectralConv3D(None, 2, modes=3, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://zongyi-li.github.io/blog/2020/fourier-pde/
        - https://arxiv.org/abs/2010.08895
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(spectral_conv_nd)


def is_lazy_call(instance, *_, **__) -> bool:
    in_features = getattr(instance, "in_features", False)
    in_size = getattr(instance, "in_size", False)
    # either in_features or in_size must is None then
    # mark the layer as lazy at call time
    return in_features is None or in_size is None


def is_lazy_init(_, in_features, *__, **k) -> bool:
    # either in_features or in_size must is None then mark the layer as lazy
    # at initialization time
    return in_features is None or k.get("in_size", False) is None


def infer_in_features(instance, x, *__, **___) -> int:
    # in case `in_features` is None, infer it from the input shape
    # otherwise return the `in_features` attribute
    if getattr(instance, "in_features", False) is None:
        return x.shape[0]
    return instance.in_features


def infer_in_size(instance, x, *__, **___) -> tuple[int, ...]:
    # in case `in_size` is None, infer it from the input shape
    # otherwise return the `in_size` attribute
    if getattr(instance, "in_size", False) is None:
        return x.shape[1:]
    return instance.in_size


updates = dict(in_features=infer_in_features, in_size=infer_in_size)


class ConvNDLocal(TreeClass):
    @ft.partial(maybe_lazy_init, is_lazy=is_lazy_init)
    def __init__(
        self,
        in_features: int | None,
        out_features: int,
        kernel_size: KernelSizeType,
        *,
        key: jax.Array,
        in_size: Sequence[int] | None,
        strides: StridesType = 1,
        padding: PaddingType = "same",
        dilation: DilationType = 1,
        weight_init: InitType = "glorot_uniform",
        bias_init: InitType = "zeros",
        dtype: DType = jnp.float32,
    ):
        self.in_features = validate_pos_int(in_features)
        self.out_features = validate_pos_int(out_features)
        self.kernel_size = canonicalize(kernel_size, self.spatial_ndim, "kernel_size")
        self.in_size = canonicalize(in_size, self.spatial_ndim, name="in_size")
        self.strides = canonicalize(strides, self.spatial_ndim, "strides")

        self.padding = delayed_canonicalize_padding(
            self.in_size,
            padding,
            self.kernel_size,
            self.strides,
        )

        self.dilation = canonicalize(dilation, self.spatial_ndim, "dilation")
        self.weight_init = weight_init
        self.bias_init = bias_init

        out_size = calculate_convolution_output_shape(
            shape=self.in_size,
            kernel_size=self.kernel_size,
            padding=self.padding,
            strides=self.strides,
        )

        # OIH...
        weight_shape = (
            self.out_features,
            self.in_features * ft.reduce(op.mul, self.kernel_size),
            *out_size,
        )

        self.weight = resolve_init(self.weight_init)(key, weight_shape, dtype)

        bias_shape = (self.out_features, *out_size)
        self.bias = resolve_init(self.bias_init)(key, bias_shape, dtype)

    @ft.partial(maybe_lazy_call, is_lazy=is_lazy_call, updates=updates)
    @ft.partial(validate_spatial_ndim, argnum=0)
    @ft.partial(validate_in_features_shape, axis=0)
    def __call__(self, input: jax.Array, mask: Weight | None = None) -> jax.Array:
        """Apply the layer.

        Args:
            input: input array. shape is ``(in_features, spatial size)``. spatial size
                is length for 1D convolution, height, width for 2D convolution and
                height, width, depth for 3D convolution.
            mask: mask to apply to the weights. shape is
                ``(out_features, in_features * prod(kernel_size), *out_size)``
                use ``None`` for no mask.
        """
        return self.conv_op(
            input=input,
            weight=self.weight,
            bias=self.bias,
            strides=self.strides,
            padding=self.padding,
            dilation=self.dilation,
            kernel_size=self.kernel_size,
            mask=mask,
        )

    spatial_ndim = property(abc.abstractmethod(lambda _: ...))
    conv_op = staticmethod(abc.abstractmethod(lambda _: ...))


class Conv1DLocal(ConvNDLocal):
    """1D Local convolutional layer.

    Local convolutional layer is a convolutional layer where the convolution
    kernel is applied to a local region of the input. The kernel weights are
    *not* shared across the spatial dimensions of the input.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        in_size: the size of the spatial dimensions of the input. e.g excluding
            the first dimension. accepts a sequence of integer(s).
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.Conv1DLocal(3, 3, 3, in_size=(32,), key=key)
        >>> l1(jnp.ones((3, 32))).shape
        (3, 32)

    Note:
        :class:`.Conv1DLocal` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv1DLocal(None, 3, 3, in_size=None, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 1
    conv_op = staticmethod(local_conv_nd)


class Conv2DLocal(ConvNDLocal):
    """2D Local convolutional layer.

    Local convolutional layer is a convolutional layer where the convolution
    kernel is applied to a local region of the input. This means that the kernel
    weights are *not* shared across the spatial dimensions of the input.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        in_size: the size of the spatial dimensions of the input. e.g excluding
            the first dimension. accepts a sequence of integer(s).
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.Conv2DLocal(3, 3, 3, in_size=(32, 32), key=key)
        >>> l1(jnp.ones((3, 32, 32))).shape
        (3, 32, 32)

    Note:
        :class:`.Conv2DLocal` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv2DLocal(None, 3, 3, in_size=None, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 2
    conv_op = staticmethod(local_conv_nd)


class Conv3DLocal(ConvNDLocal):
    """3D Local convolutional layer.

    Local convolutional layer is a convolutional layer where the convolution
    kernel is applied to a local region of the input. This means that the kernel
    weights are *not* shared across the spatial dimensions of the input.

    Args:
        in_features: Number of input feature maps, for 1D convolution this is the
            length of the input, for 2D convolution this is the number of input
            channels, for 3D convolution this is the number of input channels.
        out_features: Number of output features maps, for 1D convolution this is
            the length of the output, for 2D convolution this is the number of
            output channels, for 3D convolution this is the number of output
            channels.
        kernel_size: Size of the convolutional kernel. accepts:

            - Single integer for same kernel size in all dimensions.
            - Sequence of integers for different kernel sizes in each dimension.

        in_size: the size of the spatial dimensions of the input. e.g excluding
            the first dimension. accepts a sequence of integer(s).
        strides: Stride of the convolution. accepts:

            - Single integer for same stride in all dimensions.
            - Sequence of integers for different strides in each dimension.

        key: key to use for initializing the weights.
        padding: Padding of the input before convolution. accepts:

            - Single integer for same padding in all dimensions.
            - Sequence of integers for different padding in each dimension.
            - Sequnece of a tuple of two integers for before and after padding in
              each dimension.
            - ``same``/``SAME`` for padding such that the output has the same shape
              as the input.
            - ``valid``/``VALID`` for no padding.

        weight_init: Function to use for initializing the weights. defaults
            to ``glorot uniform``.
        bias_init: Function to use for initializing the bias. defaults to
            ``zeros``. set to ``None`` to not use a bias.
        dtype: dtype of the weights. defaults to ``float32``

    Example:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> key = jr.key(0)
        >>> l1 = sk.nn.Conv3DLocal(3, 3, 3, in_size=(32, 32, 32), key=key)
        >>> l1(jnp.ones((3, 32, 32, 32))).shape
        (3, 32, 32, 32)

    Note:
        :class:`.Conv3DLocal` supports lazy initialization, meaning that the weights and
        biases are not initialized until the first call to the layer. This is
        useful when the input shape is not known at initialization time.

        To use lazy initialization, pass ``None`` as the ``in_features`` argument
        and use :func:`.value_and_tree` to call the layer and return the method
        output and the material layer.

        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> import jax
        >>> input = jnp.ones((5, 10, 10, 10))
        >>> key = jr.key(0)
        >>> lazy = sk.nn.Conv3DLocal(None, 3, 3, in_size=None, key=key)
        >>> _, material = sk.value_and_tree(lambda lazy: lazy(input))(lazy)
        >>> print(material.in_features)
        5

    Reference:
        - https://jax.readthedocs.io/en/latest/_autosummary/jax.lax.conv.html
        - https://github.com/google/flax/blob/main/flax/linen/linear.py
    """

    spatial_ndim: int = 3
    conv_op = staticmethod(local_conv_nd)
