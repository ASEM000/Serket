# Copyright 2023 serket authors
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

from __future__ import annotations

import abc
import functools as ft
from typing import Literal, Sequence

import jax
import jax.numpy as jnp
import jax.random as jr

import serket as sk
from serket._src.custom_transform import tree_eval
from serket._src.utils import (
    IsInstance,
    Range,
    canonicalize,
    positive_int_cb,
    validate_spatial_nd,
)


def dropout_nd(
    key: jr.KeyArray,
    x: jax.Array,
    drop_rate,
    drop_axes: Sequence[int] | None = None
) -> jax.Array:
    """Drop some elements of the input array."""
    # drop_axes = None means dropout is applied to all axes
    shape = (
        x.shape
        if drop_axes is None
        else (x.shape[i] if i in drop_axes else 1 for i in range(x.ndim))
    )

    return jnp.where(
        (keep_prop := (1 - drop_rate)) == 0.0,
        jnp.zeros_like(x),
        jnp.where(jr.bernoulli(key, keep_prop, shape=shape), x / keep_prop, 0),
    )


def random_cutout_1d(
    key: jr.KeyArray,
    x: jax.Array,
    shape: tuple[int] | int,
    cutout_count: int,
    fill_value: int,
) -> jax.Array:
    """Random Cutouts for spatial 1D array.

    Args:
        x: input array
        shape: shape of the cutout
        cutout_count: number of holes. Defaults to 1.
        fill_value: fill_value to fill. Defaults to 0.
    """
    size = shape[0] if isinstance(shape, tuple) else shape
    row_arange = jnp.arange(x.shape[1])

    # split the key into subkeys, in essence, one for each cutout
    keys = jr.split(key, cutout_count)

    def scan_step(x, key):
        # define the start and end of the cutout region
        minval, maxval = 0, x.shape[1] - size
        # sample the start of the cutout region
        start = jnp.int32(jr.randint(key, shape=(), minval=minval, maxval=maxval))
        # define the mask for the cutout region
        row_mask = (row_arange >= start) & (row_arange < start + size)
        # apply the mask
        x = x * ~row_mask[None, :]
        # return the updated array as carry, skip the scan output
        return x, None

    x, _ = jax.lax.scan(scan_step, x, keys)

    return jnp.where(fill_value == 0, x, jnp.where(x == 0, fill_value, x))


def random_cutout_2d(
    key: jr.KeyArray,
    x: jax.Array,
    shape: tuple[int, int],
    cutout_count: int,
    fill_value: int,
) -> jax.Array:
    height, width = shape
    row_arange = jnp.arange(x.shape[1])
    col_arange = jnp.arange(x.shape[2])

    # split the key into `cutout_count` keys, in essence, one for each cutout
    keys = jr.split(key, cutout_count)

    def scan_step(x, key):
        # define a subkey for each dimension
        ktop, kleft = jr.split(key, 2)

        # for top define the start and end of the cutout region
        minval, maxval = 0, x.shape[1] - shape[0]
        # sample the start of the cutout region
        top = jnp.int32(jr.randint(ktop, shape=(), minval=minval, maxval=maxval))

        # for left define the start and end of the cutout region
        minval, maxval = 0, x.shape[2] - shape[1]
        left = jnp.int32(jr.randint(kleft, shape=(), minval=minval, maxval=maxval))

        # define the mask for the cutout region
        row_mask = (row_arange >= top) & (row_arange < top + height)
        col_mask = (col_arange >= left) & (col_arange < left + width)

        x = x * (~jnp.outer(row_mask, col_mask))
        return x, None

    x, _ = jax.lax.scan(scan_step, x, keys)

    return jnp.where(fill_value == 0, x, jnp.where(x == 0, fill_value, x))


@sk.autoinit
class Dropout(sk.TreeClass):
    """Drop some elements of the input array.

    Randomly zeroes some of the elements of the input array with
    probability ``drop_rate`` using samples from a Bernoulli distribution.

    Args:
        drop_rate: probability of an element to be zeroed. Default: 0.5
        drop_axes: axes to apply dropout. Default: None to apply to all axes.

    Example:
        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> layer = sk.nn.Dropout(0.5)
        >>> print(layer(jnp.ones([10]), key=jr.PRNGKey(0)))
        [2. 0. 2. 2. 2. 2. 2. 2. 0. 0.]

    Note:
        Use :func:`.tree_eval` to turn off dropout during evaluation.

        >>> import serket as sk
        >>> import jax.random as jr
        >>> linear = sk.nn.Linear(10, 10, key=jr.PRNGKey(0))
        >>> dropout = sk.nn.Dropout(0.5)
        >>> layers = sk.nn.Sequential(dropout, linear)
        >>> sk.tree_eval(layers)
        Sequential(
          layers=(
            Identity(),
            Linear(
              in_features=(10),
              out_features=10,
              weight_init=glorot_uniform,
              bias_init=zeros,
              weight=f32[10,10](μ=0.01, σ=0.31, ∈[-0.54,0.54]),
              bias=f32[10](μ=0.00, σ=0.00, ∈[0.00,0.00])
            )
          )
        )
    """

    drop_rate: float = sk.field(
        default=0.5,
        on_setattr=[IsInstance(float), Range(0, 1)],
        on_getattr=[jax.lax.stop_gradient_p.bind],
    )
    drop_axes: tuple[int, ...] | None = None

    def __call__(self, x, *, key: jr.KeyArray):
        """Drop some elements of the input array.

        Args:
            x: input array
            key: random number generator key
        """
        return dropout_nd(key, x, self.drop_rate, self.drop_axes)


@sk.autoinit
class DropoutND(sk.TreeClass):
    drop_rate: float = sk.field(
        default=0.5,
        on_setattr=[IsInstance(float), Range(0, 1)],
        on_getattr=[jax.lax.stop_gradient_p.bind],
    )

    @ft.partial(validate_spatial_nd, attribute_name="spatial_ndim")
    def __call__(self, x, *, key):
        """Drop some elements of the input array.

        Args:
            x: input array
            key: random number generator key
        """
        return dropout_nd(key, x, self.drop_rate, [0])

    @property
    @abc.abstractmethod
    def spatial_ndim(self):
        ...


class Dropout1D(DropoutND):
    """Drops full feature maps along the channel axis.

    Args:
        drop_rate: fraction of an elements to be zeroed out.

    Example:
        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> layer = sk.nn.Dropout1D(0.5)
        >>> print(layer(jnp.ones((1, 10)), key=jr.PRNGKey(0)))
        [[2. 2. 2. 2. 2. 2. 2. 2. 2. 2.]]

    Note:
        Use :func:`.tree_eval` to turn off dropout during evaluation.

        >>> import serket as sk
        >>> import jax.random as jr
        >>> linear = sk.nn.Linear(10, 10, key=jr.PRNGKey(0))
        >>> dropout = sk.nn.Dropout1D(0.5)
        >>> layers = sk.nn.Sequential(dropout, linear)
        >>> sk.tree_eval(layers)
        Sequential(
          layers=(
            Identity(),
            Linear(
              in_features=(10),
              out_features=10,
              weight_init=glorot_uniform,
              bias_init=zeros,
              weight=f32[10,10](μ=0.01, σ=0.31, ∈[-0.54,0.54]),
              bias=f32[10](μ=0.00, σ=0.00, ∈[0.00,0.00])
            )
          )
        )

    Reference:
        - https://keras.io/api/layers/regularization_layers/spatial_dropout1d/
        - https://arxiv.org/abs/1411.4280
    """

    @property
    def spatial_ndim(self) -> int:
        return 1


class Dropout2D(DropoutND):
    """Drops full feature maps along the channel axis.

    Args:
        drop_rate: fraction of an elements to be zeroed out.

    Example:
        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> layer = sk.nn.Dropout2D(0.5)
        >>> print(layer(jnp.ones((1, 5, 5)), key=jr.PRNGKey(0)))
        [[[2. 2. 2. 2. 2.]
          [2. 2. 2. 2. 2.]
          [2. 2. 2. 2. 2.]
          [2. 2. 2. 2. 2.]
          [2. 2. 2. 2. 2.]]]

    Note:
        Use :func:`.tree_eval` to turn off dropout during evaluation.

        >>> import serket as sk
        >>> import jax.random as jr
        >>> linear = sk.nn.Linear(10, 10, key=jr.PRNGKey(0))
        >>> dropout = sk.nn.Dropout2D(0.5)
        >>> layers = sk.nn.Sequential(dropout, linear)
        >>> sk.tree_eval(layers)
        Sequential(
          layers=(
            Identity(),
            Linear(
              in_features=(10),
              out_features=10,
              weight_init=glorot_uniform,
              bias_init=zeros,
              weight=f32[10,10](μ=0.01, σ=0.31, ∈[-0.54,0.54]),
              bias=f32[10](μ=0.00, σ=0.00, ∈[0.00,0.00])
            )
          )
        )

    Reference:
        - https://keras.io/api/layers/regularization_layers/spatial_dropout1d/
        - https://arxiv.org/abs/1411.4280
    """

    @property
    def spatial_ndim(self) -> int:
        return 2


class Dropout3D(DropoutND):
    """Drops full feature maps along the channel axis.

    Args:
        drop_rate: fraction of an elements to be zeroed out.

    Example:
        >>> import serket as sk
        >>> import jax.numpy as jnp
        >>> import jax.random as jr
        >>> layer = sk.nn.Dropout3D(0.5)
        >>> print(layer(jnp.ones((1, 2, 2, 2)), key=jr.PRNGKey(0)))  # doctest: +NORMALIZE_WHITESPACE
        [[[[2. 2.]
        [2. 2.]]
        <BLANKLINE>
        [[2. 2.]
        [2. 2.]]]]

    Note:
        Use :func:`.tree_eval` to turn off dropout during evaluation.

        >>> import serket as sk
        >>> import jax.random as jr
        >>> linear = sk.nn.Linear(10, 10, key=jr.PRNGKey(0))
        >>> dropout = sk.nn.Dropout3D(0.5)
        >>> layers = sk.nn.Sequential(dropout, linear)
        >>> sk.tree_eval(layers)
        Sequential(
          layers=(
            Identity(),
            Linear(
              in_features=(10),
              out_features=10,
              weight_init=glorot_uniform,
              bias_init=zeros,
              weight=f32[10,10](μ=0.01, σ=0.31, ∈[-0.54,0.54]),
              bias=f32[10](μ=0.00, σ=0.00, ∈[0.00,0.00])
            )
          )
        )

    Reference:
        - https://keras.io/api/layers/regularization_layers/spatial_dropout1d/
        - https://arxiv.org/abs/1411.4280
    """

    @property
    def spatial_ndim(self) -> int:
        return 3


class RandomCutout1D(sk.TreeClass):
    """Random Cutouts for spatial 1D array.

    Args:
        shape: shape of the cutout. accepts an int or a tuple of int.
        cutout_count: number of holes. Defaults to 1.
        fill_value: ``fill_value`` to fill the cutout region. Defaults to 0.

    Note:
        Use :func:`.tree_eval` to turn off the cutout during evaluation.

    Examples:
        >>> import jax.numpy as jnp
        >>> import serket as sk
        >>> import jax.random as jr
        >>> print(sk.nn.RandomCutout1D(5)(jnp.ones((1, 10)) * 100, key=jr.PRNGKey(0)))
        [[100. 100. 100. 100.   0.   0.   0.   0.   0. 100.]]

    Reference:
        - https://arxiv.org/abs/1708.04552
        - https://keras.io/api/keras_cv/layers/preprocessing/random_cutout/
    """

    def __init__(
        self,
        shape: int | tuple[int],
        cutout_count: int = 1,
        fill_value: int | float = 0,
    ):
        self.shape = canonicalize(shape, ndim=1, name="shape")
        self.cutout_count = positive_int_cb(cutout_count)
        self.fill_value = fill_value

    @ft.partial(validate_spatial_nd, attribute_name="spatial_ndim")
    def __call__(self, x: jax.Array, *, key: jr.KeyArray) -> jax.Array:
        """Drop some elements of the input array.

        Args:
            x: input array
            key: random number generator key
        """
        fill_value = jax.lax.stop_gradient(self.fill_value)
        out = random_cutout_1d(key, x, self.shape, self.cutout_count, fill_value)
        return out

    @property
    def spatial_ndim(self) -> int:
        return 1


class RandomCutout2D(sk.TreeClass):
    """Random Cutouts for spatial 2D array

    .. image:: ../_static/randomcutout2d.png

    Args:
        shape: shape of the cutout. accepts int or a two element tuple.
        cutout_count: number of holes. Defaults to 1.
        fill_value: ``fill_value`` to fill the cutout region. Defaults to 0.

    Note:
        Use :func:`.tree_eval` to turn off the cutout during evaluation.

    Reference:
        - https://arxiv.org/abs/1708.04552
        - https://keras.io/api/keras_cv/layers/preprocessing/random_cutout/
    """

    def __init__(
        self,
        shape: int | tuple[int, int],
        cutout_count: int = 1,
        fill_value: int | float = 0,
    ):
        self.shape = canonicalize(shape, 2, name="shape")
        self.cutout_count = positive_int_cb(cutout_count)
        self.fill_value = fill_value

    @ft.partial(validate_spatial_nd, attribute_name="spatial_ndim")
    def __call__(self, x: jax.Array, *, key: jr.KeyArray) -> jax.Array:
        fill_value = jax.lax.stop_gradient(self.fill_value)
        out = random_cutout_2d(key, x, self.shape, self.cutout_count, fill_value)
        return out

    @property
    def spatial_ndim(self) -> int:
        return 2


@tree_eval.def_eval(RandomCutout1D)
@tree_eval.def_eval(RandomCutout2D)
@tree_eval.def_eval(DropoutND)
@tree_eval.def_eval(Dropout)
def _(_) -> sk.nn.Identity:
    return sk.nn.Identity()
