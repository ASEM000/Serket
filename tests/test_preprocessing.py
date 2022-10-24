import jax.numpy as jnp
import numpy.testing as npt
import pytest

from serket.nn import HistogramEqualization2D, PixelShuffle


def test_pixel_shuffle():
    x = jnp.array(
        [
            [[0.08482574, 1.9097648], [0.29561743, 1.120948]],
            [[0.33432344, -0.82606775], [0.6481277, 1.0434873]],
            [[-0.7824839, -0.4539462], [0.6297971, 0.81524646]],
            [[-0.32787678, -1.1234448], [-1.6607416, 0.27290547]],
        ]
    )

    ps = PixelShuffle(2)
    y = jnp.array([0.08482574, 0.33432344, 1.9097648, -0.82606775])

    npt.assert_allclose(ps(x)[0, 0], y, atol=1e-5)

    with pytest.raises(AssertionError):
        PixelShuffle(3)(jnp.ones([6, 4, 4]))

    with pytest.raises(ValueError):
        PixelShuffle(-3)(jnp.ones([9, 6, 4]))


def test_histogram():
    # tested against skimage.exposure.equalize_hist

    x = jnp.array(
        [
            [
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
            ],
            [
                82,
                82,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
            ],
            [
                80,
                81,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
            ],
            [
                82,
                82,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                83,
                82,
                82,
                83,
                82,
                82,
                83,
            ],
            [
                83,
                82,
                83,
                84,
                82,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                82,
                81,
                82,
                83,
            ],
            [
                83,
                82,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                82,
                82,
                83,
                85,
                84,
                82,
                83,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                83,
                82,
                83,
                86,
                85,
                82,
                82,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                84,
                86,
                86,
                85,
                84,
                87,
                86,
                84,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                84,
                85,
                85,
                85,
                84,
                86,
                86,
                84,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                84,
                86,
                84,
                82,
                82,
                83,
                86,
                86,
                86,
                86,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                83,
                83,
                83,
                83,
                83,
                82,
                83,
                83,
                84,
                86,
                84,
                82,
                84,
                84,
                84,
                84,
                84,
                85,
            ],
            [
                83,
                83,
                83,
                82,
                83,
                82,
                82,
                83,
                83,
                83,
                83,
                83,
                81,
                84,
                82,
                83,
                86,
                84,
                82,
                85,
                86,
                82,
                82,
                82,
                84,
            ],
            [
                83,
                83,
                82,
                84,
                85,
                83,
                82,
                83,
                83,
                83,
                83,
                83,
                84,
                85,
                85,
                84,
                83,
                84,
                85,
                86,
                87,
                85,
                83,
                85,
                85,
            ],
            [
                83,
                83,
                82,
                84,
                86,
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                85,
                86,
                86,
                85,
                82,
                84,
                86,
                84,
                85,
                86,
                84,
                86,
                85,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                82,
                82,
                82,
                84,
                85,
                85,
                85,
                82,
                83,
                85,
                85,
                86,
                85,
                85,
                85,
                85,
                85,
                85,
            ],
            [
                83,
                83,
                83,
                83,
                83,
                83,
                83,
                82,
                83,
                83,
                84,
                86,
                85,
                85,
                83,
                84,
                86,
                85,
                86,
                86,
                86,
                85,
                85,
                85,
                85,
            ],
            [
                82,
                83,
                82,
                85,
                86,
                86,
                86,
                82,
                83,
                86,
                85,
                85,
                85,
                85,
                86,
                85,
                85,
                85,
                86,
                85,
                86,
                85,
                85,
                85,
                85,
            ],
            [
                84,
                83,
                82,
                84,
                84,
                83,
                83,
                82,
                83,
                86,
                85,
                85,
                85,
                85,
                85,
                86,
                85,
                85,
                86,
                85,
                86,
                85,
                85,
                85,
                85,
            ],
            [
                82,
                82,
                82,
                82,
                82,
                83,
                83,
                82,
                83,
                86,
                85,
                85,
                85,
                85,
                85,
                87,
                85,
                84,
                86,
                85,
                85,
                85,
                85,
                85,
                85,
            ],
            [
                84,
                83,
                82,
                84,
                85,
                84,
                84,
                84,
                85,
                85,
                85,
                85,
                85,
                85,
                86,
                86,
                86,
                85,
                85,
                85,
                85,
                85,
                85,
                85,
                85,
            ],
            [
                86,
                85,
                82,
                85,
                86,
                83,
                83,
                86,
                86,
                85,
                85,
                85,
                85,
                85,
                86,
                86,
                86,
                86,
                85,
                85,
                85,
                85,
                85,
                85,
                85,
            ],
            [
                85,
                85,
                85,
                85,
                86,
                85,
                85,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                85,
                85,
                85,
                85,
                86,
                86,
                86,
                86,
                84,
                85,
                87,
            ],
            [
                85,
                85,
                85,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                85,
                85,
                85,
                86,
                86,
                86,
                86,
                86,
                84,
                85,
                86,
            ],
            [
                86,
                86,
                85,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                86,
                84,
                85,
                86,
                85,
                86,
                86,
                86,
                85,
                85,
                85,
                85,
            ],
            [
                84,
                85,
                85,
                85,
                85,
                85,
                85,
                85,
                86,
                86,
                86,
                86,
                85,
                86,
                85,
                85,
                85,
                85,
                85,
                86,
                85,
                85,
                85,
                85,
                85,
            ],
        ]
    )

    y = jnp.array(
        [
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
            ],
            [
                48.552,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
            ],
            [
                0.408,
                1.632,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
            ],
            [
                48.552,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                48.552,
                48.552,
                133.824,
                48.552,
                48.552,
                133.824,
            ],
            [
                133.824,
                48.552,
                133.824,
                151.776,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                1.632,
                48.552,
                133.824,
            ],
            [
                133.824,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                211.344,
                151.776,
                48.552,
                133.824,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                48.552,
                133.824,
                253.368,
                211.344,
                48.552,
                48.552,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                151.776,
                253.368,
                253.368,
                211.344,
                151.776,
                255.0,
                253.368,
                151.776,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                151.776,
                211.344,
                211.344,
                211.344,
                151.776,
                253.368,
                253.368,
                151.776,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                151.776,
                253.368,
                151.776,
                48.552,
                48.552,
                133.824,
                253.368,
                253.368,
                253.368,
                253.368,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                133.824,
                133.824,
                151.776,
                253.368,
                151.776,
                48.552,
                151.776,
                151.776,
                151.776,
                151.776,
                151.776,
                211.344,
            ],
            [
                133.824,
                133.824,
                133.824,
                48.552,
                133.824,
                48.552,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                1.632,
                151.776,
                48.552,
                133.824,
                253.368,
                151.776,
                48.552,
                211.344,
                253.368,
                48.552,
                48.552,
                48.552,
                151.776,
            ],
            [
                133.824,
                133.824,
                48.552,
                151.776,
                211.344,
                133.824,
                48.552,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                151.776,
                211.344,
                211.344,
                151.776,
                133.824,
                151.776,
                211.344,
                253.368,
                255.0,
                211.344,
                133.824,
                211.344,
                211.344,
            ],
            [
                133.824,
                133.824,
                48.552,
                151.776,
                253.368,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                211.344,
                253.368,
                253.368,
                211.344,
                48.552,
                151.776,
                253.368,
                151.776,
                211.344,
                253.368,
                151.776,
                253.368,
                211.344,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                48.552,
                48.552,
                48.552,
                151.776,
                211.344,
                211.344,
                211.344,
                48.552,
                133.824,
                211.344,
                211.344,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                133.824,
                48.552,
                133.824,
                133.824,
                151.776,
                253.368,
                211.344,
                211.344,
                133.824,
                151.776,
                253.368,
                211.344,
                253.368,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                48.552,
                133.824,
                48.552,
                211.344,
                253.368,
                253.368,
                253.368,
                48.552,
                133.824,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                211.344,
                211.344,
                211.344,
                253.368,
                211.344,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                151.776,
                133.824,
                48.552,
                151.776,
                151.776,
                133.824,
                133.824,
                48.552,
                133.824,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                211.344,
                211.344,
                253.368,
                211.344,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                48.552,
                48.552,
                48.552,
                48.552,
                48.552,
                133.824,
                133.824,
                48.552,
                133.824,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                255.0,
                211.344,
                151.776,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                151.776,
                133.824,
                48.552,
                151.776,
                211.344,
                151.776,
                151.776,
                151.776,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                253.368,
                211.344,
                48.552,
                211.344,
                253.368,
                133.824,
                133.824,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                151.776,
                211.344,
                255.0,
            ],
            [
                211.344,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                151.776,
                211.344,
                253.368,
            ],
            [
                253.368,
                253.368,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                253.368,
                151.776,
                211.344,
                253.368,
                211.344,
                253.368,
                253.368,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
            [
                151.776,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                253.368,
                253.368,
                253.368,
                211.344,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
                253.368,
                211.344,
                211.344,
                211.344,
                211.344,
                211.344,
            ],
        ]
    )

    npt.assert_allclose(y, HistogramEqualization2D()(x[None])[0], atol=1e-3)
