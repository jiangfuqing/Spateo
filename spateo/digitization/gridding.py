"""Written by @Jinerhal, adapted by @Xiaojieqiu.
"""

from typing import List, Optional, Tuple, Union

import cv2
import numpy as np
from anndata import AnnData
from nptyping import NDArray

from .utils import *


def digitize(
    adata: AnnData,
    ctrs,
    ctr_idx: int,
    pnt_xy: Tuple[int, int],
    pnt_Xy: Tuple[int, int],
    pnt_xY: Tuple[int, int],
    pnt_XY: Tuple[int, int],
    spatial_key: str = "spatial",
    dgl_layer_key: str = "digital_layer",
    dgl_column_key: str = "digital_column",
    lp: float = 1,
    hp: float = 100,
) -> None:
    """Calculate weights for digitization in the area of interest, given by four user
       defined coordinates. Digitization weights will be used for grid segmentation.

    Args:
        adata (AnnData): The adata object to digitize.
        ctrs (_type_): Contours generated by `cv2.findContours`.
        ctr_idx (int): Contour index for given points.
        pnt_xy (Tuple[int, int]): Corner point to define an area of interest.
            pnt_xy correspond to the minimal value in both columns and layers.
        pnt_Xy (Tuple[int, int]): Corner point correspond to the maximal value
            in columns and minimal in layers.
        pnt_xY (Tuple[int, int]): Corner point correspond to the minimal value
            in columns and maximal in layers.
        pnt_XY (Tuple[int, int]): Corner point correspond to the maximal value
            in both columns and layers.
        spatial_key (str, optional): The key name of the spatial coordinates. Defaults to "spatial".
        dgl_layer_key (str, optional): The key name to store layer digital-weights. Defaults to "digital_layer".
        dgl_column_key (str, optional): The key name to store column digital-weights.
        lp (float, optional): lowest digital-weight (or potential). Defaults to 1.
        hp (float, optional): highest digital-weight (or potentil). Defaults to 100.
    """

    empty_field = np.zeros((int(max(adata.obsm[spatial_key][:, 0])) + 1, int(max(adata.obsm[spatial_key][:, 1])) + 1))

    field_border = np.zeros_like(empty_field)
    cv2.drawContours(field_border, ctrs, ctr_idx, ctr_idx + 1, 1)
    field_mask = np.zeros_like(empty_field)
    cv2.drawContours(field_mask, ctrs, ctr_idx, ctr_idx + 1, cv2.FILLED)

    min_line_l, max_line_l, min_line_c, max_line_c = field_contours(ctrs[ctr_idx], pnt_xy, pnt_Xy, pnt_xY, pnt_XY)

    of_layer = calc_op_field(
        empty_field, min_line_l, max_line_l, min_line_c, max_line_c, field_border, field_mask, lp=lp, hp=hp
    )
    adata.obs[dgl_layer_key] = 0
    for i in range(len(adata)):
        adata.obs[dgl_layer_key][i] = of_layer[int(adata.obsm[spatial_key][i, 0]), int(adata.obsm[spatial_key][i, 1])]

    of_column = calc_op_field(
        empty_field, min_line_c, max_line_c, min_line_l, max_line_l, field_border, field_mask, lp=lp, hp=hp
    )
    adata.obs[dgl_column_key] = 0
    for i in range(len(adata)):
        adata.obs[dgl_column_key][i] = of_column[int(adata.obsm[spatial_key][i, 0]), int(adata.obsm[spatial_key][i, 1])]


def soft_gridding(
    adata,
    layer_num: int,
    column_num: int,
    layer_border_width: int = 2,
    column_border_width: int = 2,
    layer_label_key: str = "layer_label",
    column_label_key: str = "column_label",
    grid_label_key: str = "grid_label",
    dgl_layer_key: str = "digital_layer",
    dgl_column_key: str = "digital_column",
    lp: int = 1,
    hp: int = 100,
) -> None:
    """Segment the area of interests into given number of layers/columns, according to
        pre-calculated digitization weights.

    Args:
        adata (AnnData): The adata object to do grid segmentation.
        layer_num (int): Number of layers to segment.
        column_num (int): Number of columns to segment.
        layer_border_width (int, optional): Layer boundary width. Only affect grid_label.
        column_border_width (int, optional): Column boundary width. Only affect grid_label.
        layer_label_key (str, optional): The key name to store layer labels. Defaults to "layer_label".
        column_label_key (str, optional): The key name to store column labels. Defaults to "column_label".
        grid_label_key (str, optional): The key name to store grid labels. Defaults to "grid_label".
        dgl_layer_key (str, optional): The key name of layer digitization weights. Defaults to "digital_layer".
        dgl_column_key (str, optional): The key name of column digitization weights. Defaults to "digital_column".
        lp (int, optional): lowest digi-weight. Defaults to 1.
        hp (int, optional): highest digi-weight. Defaults to 100.

    Returns:
        None
    """

    adata.obs[layer_label_key] = 0
    adata.obs[column_label_key] = 0

    adata.obs[grid_label_key] = "NA"
    adata.obs[grid_label_key][adata.obs[dgl_layer_key] != 0] = "Grid Area"
    adata.obs[grid_label_key][adata.obs[dgl_column_key] != 0] = "Grid Area"

    region_mask = adata.obs[grid_label_key].copy()
    region_mask[region_mask == "Grid Area"] = "Region Boundary"

    value_list = np.linspace(lp, hp, layer_num + 1)
    for i in range(len(value_list) - 1):
        # fast method to identify within a list of range
        adata.obs[layer_label_key] = np.where(
            (adata.obs[dgl_layer_key] > value_list[i]) & (adata.obs[dgl_layer_key] <= value_list[i + 1]),
            i + 1,
            adata.obs[layer_label_key],
        )
        adata.obs[grid_label_key] = np.where(
            (adata.obs[dgl_layer_key] > (value_list[i] - layer_border_width / 2))
            & (adata.obs[dgl_layer_key] <= (value_list[i] + layer_border_width / 2)),
            region_mask,
            adata.obs[grid_label_key],
        )

    value_list = np.linspace(lp, hp, column_num + 1)
    for i in range(len(value_list) - 1):
        adata.obs[column_label_key] = np.where(
            (adata.obs[dgl_column_key] > value_list[i]) & (adata.obs[dgl_column_key] <= value_list[i + 1]),
            i + 1,
            adata.obs[column_label_key],
        )
        adata.obs[grid_label_key] = np.where(
            (adata.obs[dgl_column_key] > (value_list[i] - column_border_width / 2))
            & (adata.obs[dgl_column_key] <= (value_list[i] + column_border_width / 2)),
            region_mask,
            adata.obs[grid_label_key],
        )
