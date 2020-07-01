# Copyright (C) 2017-2020 Trent Houliston <trent@houliston.me>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import tensorflow as tf


class Image:
    def __init__(self, **config):
        self.variations = {} if "variations" not in config else config["variations"]

    def _interpolate_gather(self, img, C):

        # Get our four surrounding coordinates
        y_0 = tf.math.floor(C[:, 0])
        x_0 = tf.math.floor(C[:, 1])
        y_1 = y_0 + 1
        x_1 = x_0 + 1

        # Get the coordinates of the four closest pixels to this point making sure we clip to the edge of the screen
        corners = [
            tf.clip_by_value(tf.cast(tf.stack([a, b], axis=-1), tf.int32), [[0, 0]], [tf.shape(img)[:2] - 1])
            for a, b in [(y_0, x_0), (y_0, x_1), (y_1, x_0), (y_1, x_1)]
        ]

        # Calculate the weights of how much the x and y account for for each of the 4 corners
        y_w = C[:, 0] - y_0
        x_w = C[:, 1] - x_0

        # Gather the pixel values from the image
        p_val = [tf.gather_nd(img, idx) for idx in corners]

        # Weight each of the pixel values based on their relative distance
        p_weighted = [
            tf.multiply(val, tf.expand_dims(w, axis=-1))
            for val, w in zip(
                p_val,
                [
                    tf.multiply(1.0 - y_w, 1.0 - x_w),
                    tf.multiply(1.0 - y_w, x_w),
                    tf.multiply(y_w, 1.0 - x_w),
                    tf.multiply(y_w, x_w),
                ],
            )
        ]

        # Add all the weighted values to get the final interpolated value
        return tf.add_n(p_weighted)

    def features(self):
        return {
            "image": tf.io.FixedLenFeature([], tf.string),
        }

    def input(self, image, **features):

        # Return the image and the original compressed image
        return {
            "jpg": image,
            "image": tf.image.convert_image_dtype(
                tf.image.decode_image(image, channels=3, expand_animations=False), tf.float32
            ),
        }

    def __call__(self, image, C, **features):

        # Apply the variants that were listed
        if "brightness" in self.variations:
            v = self.variations["brightness"]
            image = tf.image.adjust_brightness(image, tf.random.truncated_normal(shape=(), **v))
        if "contrast" in self.variations:
            v = self.variations["contrast"]
            image = tf.image.adjust_contrast(image, tf.random.truncated_normal(shape=(), **v))
        if "hue" in self.variations:
            v = self.variations["hue"]
            image = tf.image.adjust_hue(image, tf.random.truncated_normal(shape=(), **v))
        if "saturation" in self.variations:
            v = self.variations["saturation"]
            image = tf.image.adjust_saturation(image, tf.random.truncated_normal(shape=(), **v))
        if "gamma" in self.variations:
            v = self.variations["gamma"]
            image = tf.image.adjust_gamma(image, tf.random.truncated_normal(shape=(), **v))

        # Get the pixels referenced by the image
        return {
            "X": self._interpolate_gather(image, C),
        }