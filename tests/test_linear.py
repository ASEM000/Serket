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

import jax
import jax.numpy as jnp
import numpy.testing as npt
import pytest

import serket as sk


def test_embed():
    table = sk.nn.Embedding(10, 3, key=jax.random.key(0))
    x = jnp.array([9])
    assert table(x).shape == (1, 3)

    with pytest.raises(TypeError):
        table(jnp.array([9.0]))


def test_identity():
    x = jnp.array([[1, 2, 3], [4, 5, 6]])
    layer = sk.nn.Identity()
    npt.assert_allclose(x, layer(x))


@pytest.mark.parametrize(
    "in_features,out_features,in_axis,out_axis,desired_shape",
    [
        # single feature
        (1, 10, 0, 0, (10, 2, 3, 4, 5, 6)),
        (1, 10, 0, -6, (10, 2, 3, 4, 5, 6)),
        (1, 10, 0, -1, (2, 3, 4, 5, 6, 10)),
        # multiple in features single out feature
        ([1, 2], 10, [0, 1], 0, (10, 3, 4, 5, 6)),
        ([1, 2], 10, [0, 1], -3, (3, 4, 10, 5, 6)),
        ([1, 2], 10, [0, 1], -1, (3, 4, 5, 6, 10)),
        # single in features multiple out features
        (1, [8, 9], 0, [0, 1], (8, 9, 2, 3, 4, 5, 6)),
        (2, [8, 9], 1, [0, 1], (8, 9, 1, 3, 4, 5, 6)),
        # multiple in/out features
        ([2, 3], [8, 9], [1, 2], [1, 2], (1, 8, 9, 4, 5, 6)),
    ],
)
def test_general_linear_shape(
    in_features,
    out_features,
    in_axis,
    out_axis,
    desired_shape,
):
    #             0  1  2  3  4  5
    x = jnp.ones([1, 2, 3, 4, 5, 6])
    layer = sk.nn.Linear(
        in_features=in_features,
        in_axis=in_axis,
        out_features=out_features,
        out_axis=out_axis,
        key=jax.random.key(0),
    )
    assert layer(x).shape == desired_shape


def test_linear():
    x = jnp.ones([1, 2, 3, 4])
    layer = sk.nn.Linear(
        in_features=(1, 2),
        in_axis=(0, 1),
        out_features=5,
        key=jax.random.key(0),
    )
    assert layer(x).shape == (3, 4, 5)

    x = jnp.ones([1, 2, 3, 4])
    layer = sk.nn.Linear(
        in_features=(1, 2),
        in_axis=(0, -3),
        out_features=5,
        key=jax.random.key(0),
    )
    assert layer(x).shape == (3, 4, 5)

    x = jnp.ones([1, 2, 3, 4])
    layer = sk.nn.Linear(
        in_features=(2, 3),
        in_axis=(1, -2),
        out_features=5,
        key=jax.random.key(0),
    )
    assert layer(x).shape == (1, 4, 5)

    with pytest.raises(ValueError):
        sk.nn.Linear(
            in_features=2,
            in_axis=(1, -2),
            out_features=5,
            key=jax.random.key(0),
        )

    with pytest.raises(ValueError):
        sk.nn.Linear(
            in_features=(2, 3),
            in_axis=2,
            out_features=5,
            key=jax.random.key(0),
        )

    with pytest.raises(ValueError):
        sk.nn.Linear(
            in_features=(1,),
            in_axis=(0, -3),
            out_features=5,
            key=jax.random.key(0),
        )

    with pytest.raises(TypeError):
        sk.nn.Linear(
            in_features=(1, "s"),
            in_axis=(0, -3),
            out_features=5,
            key=jax.random.key(0),
        )

    with pytest.raises(TypeError):
        sk.nn.Linear(
            in_features=(1, 2),
            in_axis=(0, "s"),
            out_features=3,
            key=jax.random.key(0),
        )


def test_mlp():
    x = jnp.linspace(0, 1, 100)[:, None]

    x = jax.random.normal(jax.random.key(0), (10, 1))
    w1 = jax.random.normal(jax.random.key(1), (1, 10))
    w2 = jax.random.normal(jax.random.key(2), (10, 10))
    w3 = jax.random.normal(jax.random.key(3), (10, 4))

    y = x @ w1
    y = jax.nn.tanh(y)
    y = y @ w2
    y = jax.nn.tanh(y)
    y = y @ w3

    layer = sk.nn.MLP(
        1,
        4,
        hidden_features=10,
        num_hidden_layers=2,
        act="tanh",
        bias_init=None,
        key=jax.random.key(0),
    )

    layer = layer.at["in_weight"].set(w1.T)
    layer = layer.at["mid_weight"].set(w2.T[None])
    layer = layer.at["out_weight"].set(w3.T)

    npt.assert_allclose(layer(x), y)


def test_mlp_bias():
    x = jnp.linspace(0, 1, 100)[:, None]

    x = jax.random.normal(jax.random.key(0), (10, 1))
    w1 = jax.random.normal(jax.random.key(1), (1, 10))
    w2 = jax.random.normal(jax.random.key(2), (10, 10))
    w3 = jax.random.normal(jax.random.key(3), (10, 4))
    b1 = jax.random.normal(jax.random.key(4), (10,))
    b2 = jax.random.normal(jax.random.key(5), (10,))
    b3 = jax.random.normal(jax.random.key(6), (4,))

    y = x @ w1 + b1
    y = jax.nn.tanh(y)
    y = y @ w2 + b2
    y = jax.nn.tanh(y)
    y = y @ w3 + b3

    layer = sk.nn.MLP(
        1,
        4,
        hidden_features=10,
        num_hidden_layers=2,
        act="tanh",
        bias_init="zeros",
        key=jax.random.key(0),
    )

    layer = layer.at["in_weight"].set(w1.T)
    layer = layer.at["in_bias"].set(b1)
    layer = layer.at["mid_weight"].set(w2.T[None])
    layer = layer.at["mid_bias"].set(b2[None])
    layer = layer.at["out_weight"].set(w3.T)
    layer = layer.at["out_bias"].set(b3)

    npt.assert_allclose(layer(x), y)
