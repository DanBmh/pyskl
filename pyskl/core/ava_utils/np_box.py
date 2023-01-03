# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
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
"""Operations for [N, 4] numpy arrays representing bounding boxes.

Example box operations that are supported:
    * Areas: compute bounding box areas
    * IOU: pairwise intersection-over-union scores
"""

import numpy as np


def area(boxes):
    """Computes area of boxes.

    Args:
        boxes: Numpy array with shape [N, 4] holding N boxes

    Returns:
        a numpy array with shape [N*1] representing box areas
    """
    return (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])


def intersection(boxes1, boxes2):
    """Compute pairwise intersection areas between boxes.

    Args:
        boxes1: a numpy array with shape [N, 4] holding N boxes
        boxes2: a numpy array with shape [M, 4] holding M boxes

    Returns:
        a numpy array with shape [N*M] representing pairwise intersection area
    """
    [y_min1, x_min1, y_max1, x_max1] = np.split(boxes1, 4, axis=1)
    [y_min2, x_min2, y_max2, x_max2] = np.split(boxes2, 4, axis=1)

    all_pairs_min_ymax = np.minimum(y_max1, np.transpose(y_max2))
    all_pairs_max_ymin = np.maximum(y_min1, np.transpose(y_min2))
    intersect_heights = np.maximum(
        np.zeros(all_pairs_max_ymin.shape),
        all_pairs_min_ymax - all_pairs_max_ymin)
    all_pairs_min_xmax = np.minimum(x_max1, np.transpose(x_max2))
    all_pairs_max_xmin = np.maximum(x_min1, np.transpose(x_min2))
    intersect_widths = np.maximum(
        np.zeros(all_pairs_max_xmin.shape),
        all_pairs_min_xmax - all_pairs_max_xmin)
    return intersect_heights * intersect_widths


def iou(boxes1, boxes2):
    """Computes pairwise intersection-over-union between box collections.

    Args:
        boxes1: a numpy array with shape [N, 4] holding N boxes.
        boxes2: a numpy array with shape [M, 4] holding N boxes.

    Returns:
        a numpy array with shape [N, M] representing pairwise iou scores.
    """
    intersect = intersection(boxes1, boxes2)
    area1 = area(boxes1)
    area2 = area(boxes2)
    union = (
        np.expand_dims(area1, axis=1) + np.expand_dims(area2, axis=0) -
        intersect)
    return intersect / union


class BoxList:
    """Box collection.

    BoxList represents a list of bounding boxes as numpy array, where each
    bounding box is represented as a row of 4 numbers,
    [y_min, x_min, y_max, x_max].  It is assumed that all bounding boxes within
    a given list correspond to a single image.

    Optionally, users can add additional related fields (such as
    objectness/classification scores).
    """

    def __init__(self, data):
        """Constructs box collection.

        Args:
            data: a numpy array of shape [N, 4] representing box coordinates

        Raises:
            ValueError: if bbox data is not a numpy array
            ValueError: if invalid dimensions for bbox data
        """
        if not isinstance(data, np.ndarray):
            raise ValueError('data must be a numpy array.')
        if len(data.shape) != 2 or data.shape[1] != 4:
            raise ValueError('Invalid dimensions for box data.')
        if data.dtype != np.float32 and data.dtype != np.float64:
            raise ValueError(
                'Invalid data type for box data: float is required.')
        if not self._is_valid_boxes(data):
            raise ValueError('Invalid box data. data must be a numpy array of '
                             'N*[y_min, x_min, y_max, x_max]')
        self.data = {'boxes': data}

    def num_boxes(self):
        """Return number of boxes held in collections."""
        return self.data['boxes'].shape[0]

    def get_extra_fields(self):
        """Return all non-box fields."""
        return [k for k in self.data if k != 'boxes']

    def has_field(self, field):
        return field in self.data

    def add_field(self, field, field_data):
        """Add data to a specified field.

        Args:
            field: a string parameter used to specify a related field to be
                accessed.
            field_data: a numpy array of [N, ...] representing the data
                associated with the field.
        Raises:
            ValueError: if the field is already exist or the dimension of the
                field data does not matches the number of boxes.
        """
        if self.has_field(field):
            raise ValueError('Field ' + field + 'already exists')
        if len(field_data.shape) < 1 or field_data.shape[0] != self.num_boxes(
        ):
            raise ValueError('Invalid dimensions for field data')
        self.data[field] = field_data

    def get(self):
        """Convenience function for accesssing box coordinates.

        Returns:
            a numpy array of shape [N, 4] representing box corners
        """
        return self.get_field('boxes')

    def get_field(self, field):
        """Accesses data associated with the specified field in the box
        collection.

        Args:
            field: a string parameter used to specify a related field to be
                accessed.

        Returns:
            a numpy 1-d array representing data of an associated field

        Raises:
            ValueError: if invalid field
        """
        if not self.has_field(field):
            raise ValueError(f'field {field} does not exist')
        return self.data[field]

    def get_coordinates(self):
        """Get corner coordinates of boxes.

        Returns:
            a list of 4 1-d numpy arrays [y_min, x_min, y_max, x_max]
        """
        box_coordinates = self.get()
        y_min = box_coordinates[:, 0]
        x_min = box_coordinates[:, 1]
        y_max = box_coordinates[:, 2]
        x_max = box_coordinates[:, 3]
        return [y_min, x_min, y_max, x_max]

    @staticmethod
    def _is_valid_boxes(data):
        """Check whether data fulfills the format of N*[ymin, xmin, ymax,
        xmin].

        Args:
            data: a numpy array of shape [N, 4] representing box coordinates

        Returns:
            a boolean indicating whether all ymax of boxes are equal or greater
            than ymin, and all xmax of boxes are equal or greater than xmin.
        """
        if len(data) != 0:
            for v in data:
                if v[0] > v[2] or v[1] > v[3]:
                    return False
        return True