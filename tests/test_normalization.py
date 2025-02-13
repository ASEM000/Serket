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

import os

import jax
import jax.numpy as jnp
import numpy.testing as npt
import pytest

import serket as sk

os.environ["KERAS_BACKEND"] = "jax"


def test_layer_norm():
    layer = sk.nn.LayerNorm(
        (5, 2), bias_init=None, weight_init=None, key=jax.random.key(0)
    )

    x = jnp.array(
        [
            [10, 12],
            [20, 22],
            [30, 32],
            [40, 42],
            [50, 52],
        ]
    )

    y = jnp.array(
        [
            [-1.4812257, -1.3401566],
            [-0.7758801, -0.63481104],
            [-0.07053456, 0.07053456],
            [0.63481104, 0.7758801],
            [1.3401566, 1.4812257],
        ]
    )

    npt.assert_allclose(layer(x), y, atol=1e-5)


def test_instance_norm():
    x = jnp.array(
        [
            [
                [-1.1472481, -1.9474537, -2.0057163, 0.6425913, 0.36222667],
                [-1.2547379, 0.9062948, 0.17921783, 1.3880836, -0.27561226],
                [-1.257894, -0.8935803, 1.2161034, 0.19008707, 1.4399774],
                [1.3984185, -1.0334028, -0.24350524, -1.1016859, -2.2860343],
            ],
            [
                [0.92866606, 0.25624245, 0.9682049, -0.67891, 0.8530893],
                [0.33257663, -0.74466753, -0.04999696, 0.69808286, 1.4251236],
                [-0.36604142, 1.1731377, 0.75704926, -0.24038437, -1.0581506],
                [1.3532404, -0.87451035, -1.0073394, -1.9598479, -0.05853728],
            ],
            [
                [1.2020007, -0.02205517, -0.31324545, 0.7623346, -1.2435328],
                [-0.15989126, -1.48976, -1.0510173, -1.25619, -0.81498605],
                [0.4084815, -0.32708976, -0.57680005, -0.7171833, 1.6620917],
                [-1.5612144, 1.6252923, -1.5041701, -2.2276561, 1.5729015],
            ],
        ]
    )

    y = jnp.array(
        [
            [
                [-0.728241, -1.405019, -1.454294, 0.785524, 0.548404],
                [-0.819151, 1.008553, 0.393624, 1.416028, 0.008949],
                [-0.821820, -0.513700, 1.270575, 0.402817, 1.459917],
                [1.424768, -0.631956, 0.036104, -0.689707, -1.691375],
            ],
            [
                [0.919841, 0.186398, 0.962967, -0.833614, 0.837406],
                [0.269660, -0.905338, -0.147630, 0.668333, 1.461349],
                [-0.492354, 1.186496, 0.732651, -0.355294, -1.247268],
                [1.382943, -1.046964, -1.191846, -2.230789, -0.156946],
            ],
            [
                [1.316069, 0.244669, -0.010207, 0.931235, -0.824476],
                [0.124022, -1.039995, -0.655969, -0.835554, -0.449374],
                [0.621512, -0.022324, -0.240893, -0.363768, 1.718781],
                [-1.102538, 1.686571, -1.052608, -1.685866, 1.640714],
            ],
        ]
    )

    layer = sk.nn.InstanceNorm(in_features=3, key=jax.random.key(0))

    npt.assert_allclose(layer(x), y, atol=1e-5)

    layer = sk.nn.InstanceNorm(
        in_features=3, weight_init=None, bias_init=None, key=jax.random.key(0)
    )

    npt.assert_allclose(layer(x), y, atol=1e-5)


def test_group_norm():
    x = jnp.array(
        [
            [
                [-0.63612133, 0.19765279, 2.1146476],
                [0.031331, -1.311904, -0.0374171],
                [-0.54120636, -0.6456455, 0.9654913],
                [0.2920794, -0.22726963, -0.24639332],
                [0.72095776, -1.870035, -0.8900444],
            ],
            [
                [-1.4811108, 1.2653948, 0.5540175],
                [0.364565, -2.3408854, 0.38399327],
                [-0.17656331, -0.3442401, -0.3825781],
                [-1.4098688, -0.6070761, -1.3587425],
                [0.28523827, -2.6206584, -0.560204],
            ],
            [
                [1.0689651, -0.36957648, 0.03958316],
                [0.6426313, 1.6226007, 0.33550736],
                [1.5684571, 0.20733729, -0.21399988],
                [-2.2599938, 0.03160276, 0.47916695],
                [0.949695, -0.38839674, -0.15436219],
            ],
            [
                [-1.7157102, -1.058211, 0.02331979],
                [1.6036886, -0.40189204, 0.43017793],
                [0.26428807, -1.145074, -1.9501098],
                [-0.02625608, -1.1551405, 0.9615113],
                [-0.13956395, 0.52319974, -0.19498196],
            ],
            [
                [-0.46221837, 0.21079917, -1.775888],
                [0.11359276, 0.76481146, -0.7664055],
                [0.12426665, -0.09239463, 0.5783123],
                [0.5898865, -0.9269353, -1.0334642],
                [-1.0781585, 0.6940988, -0.2026084],
            ],
            [
                [0.3588827, 0.8591659, -1.0595357],
                [-0.9108115, -1.0176793, 0.2658015],
                [0.989668, 0.10990606, -0.6376366],
                [0.19915614, 0.98604953, -0.680729],
                [0.573196, 0.33570245, -0.67793137],
            ],
        ]
    )

    y = jnp.array(
        [
            [
                [-4.7004575e-01, 3.4372184e-01, 2.2147183e00],
                [1.8139099e-01, -1.1296129e00, 1.1429250e-01],
                [-3.7740827e-01, -4.7934139e-01, 1.0931360e00],
                [4.3588269e-01, -7.1004502e-02, -8.9669317e-02],
                [8.5447007e-01, -1.6743517e00, -7.1787590e-01],
            ],
            [
                [-1.2947596e00, 1.3858433e00, 6.9153559e-01],
                [5.0662905e-01, -2.1339037e00, 5.2559108e-01],
                [-2.1514880e-02, -1.8516825e-01, -2.2258633e-01],
                [-1.2252271e00, -4.4169748e-01, -1.1753275e00],
                [4.2920575e-01, -2.4069636e00, -3.9595008e-01],
            ],
            [
                [1.1941268e00, -2.0989668e-01, 1.8944514e-01],
                [7.7802306e-01, 1.7344779e00, 4.7826859e-01],
                [1.6816336e00, 3.5317397e-01, -5.8053162e-02],
                [-2.0549531e00, 1.8165621e-01, 6.1848104e-01],
                [1.0777187e00, -2.2826535e-01, 1.5352231e-04],
            ],
            [
                [-1.8924645e00, -1.0886236e00, 2.3362677e-01],
                [2.1657434e00, -2.8622580e-01, 7.3104066e-01],
                [5.2822810e-01, -1.1948200e00, -2.1790352e00],
                [1.7301665e-01, -1.2071271e00, 1.3806345e00],
                [3.4489498e-02, 8.4476656e-01, -3.3263080e-02],
            ],
            [
                [-3.5997915e-01, 4.6283406e-01, -1.9660363e00],
                [3.4399211e-01, 1.1401546e00, -7.3187023e-01],
                [3.5704172e-01, 9.2157446e-02, 9.1214573e-01],
                [9.2629600e-01, -9.2812955e-01, -1.0583689e00],
                [-1.1130110e00, 1.0537031e00, -4.2586964e-02],
            ],
            [
                [6.4387697e-01, 1.2555099e00, -1.0902433e00],
                [-9.0841711e-01, -1.0390707e00, 5.3007841e-01],
                [1.4150583e00, 3.3948481e-01, -5.7444078e-01],
                [4.4859958e-01, 1.4106344e00, -6.2712437e-01],
                [9.0589064e-01, 6.1553746e-01, -6.2370408e-01],
            ],
        ]
    )

    layer = sk.nn.GroupNorm(in_features=6, groups=2, key=jax.random.key(0))

    npt.assert_allclose(layer(x), y, atol=1e-5)

    with pytest.raises(ValueError):
        layer = sk.nn.GroupNorm(in_features=6, groups=4, key=jax.random.key(0))

    with pytest.raises(ValueError):
        layer = sk.nn.GroupNorm(in_features=0, groups=1, key=jax.random.key(0))

    with pytest.raises(ValueError):
        layer = sk.nn.GroupNorm(in_features=-1, groups=0, key=jax.random.key(0))


@pytest.mark.parametrize(
    ["axis", "axis_name"],
    [[0, None], [1, "foo"], [2, "bar"], [3, "baz"]],
)
def test_batchnorm(axis, axis_name):
    import math

    from keras.layers import BatchNormalization

    mat_jax = lambda n: jnp.arange(1, math.prod(n) + 1).reshape(*n).astype(jnp.float32)

    x_keras = mat_jax((5, 10, 7, 8))

    bn_keras = BatchNormalization(
        axis=axis,
        momentum=0.5,
        center=False,
        scale=False,
        epsilon=1e-7,
    )

    for i in range(5):
        x_keras = bn_keras(x_keras, training=True)

    bn_sk = sk.nn.BatchNorm(
        x_keras.shape[axis],
        momentum=0.5,
        eps=bn_keras.epsilon,
        axis=axis,
        bias_init=None,
        weight_init=None,
        axis_name=axis_name,
        key=jax.random.key(0),
    )
    state = sk.tree_state(bn_sk)
    x_sk = mat_jax((5, 10, 7, 8))
    in_axes = (0, None)
    out_axes = (0, None)
    kwargs = dict(axis_name=axis_name, in_axes=in_axes, out_axes=out_axes)
    if axis_name is None:
        kwargs.pop("axis_name")
    for _ in range(5):
        x_sk, state = jax.vmap(bn_sk, **kwargs)(x_sk, state)

    npt.assert_allclose(x_keras, x_sk, atol=1e-5)
    npt.assert_allclose(bn_keras.moving_mean, state.running_mean, atol=1e-5)
    npt.assert_allclose(bn_keras.moving_variance, state.running_var, rtol=1e-4)
    x_keras = bn_keras(x_keras, training=False)
    bn_sk_eval = sk.tree_eval(bn_sk)
    in_axes = (0, None)
    x_sk, _ = jax.vmap(bn_sk_eval, in_axes, axis_name=axis_name)(x_sk, state)
    npt.assert_allclose(x_keras, x_sk, rtol=1e-4)


def test_weight_norm_wrapper():
    weight: jax.Array = jnp.array(
        [
            [-1.2662824, 0.6269297, 0.35720623, 0.04510251],
            [0.557601, 0.11622565, -0.27115023, -0.19996592],
        ],
    )
    linear = sk.nn.Linear(2, 4, key=jax.random.key(0))
    linear = linear.at["weight"].set(sk.nn.weight_norm(weight).T)
    true = jnp.array([[-0.51219565, 1.1655288, 0.19189113, -0.7554708]])
    pred = linear(jnp.ones((1, 2)))
    npt.assert_allclose(true, pred, atol=1e-5)
