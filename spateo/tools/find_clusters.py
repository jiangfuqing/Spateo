from .find_clusters_utils import *
import numpy as np
import pandas as pd
import random, torch
import cv2


def find_cluster_spagcn(
    adata,
    n_clusters,
    p=0.5,
    s=1,
    b=49,
    refine_shape=None,
    his_img_path=None,
    total_umi=None,
    x_pixel="x_pixel",
    y_pixel="y_pixel",
    x_array="x_array",
    y_array="y_array",
    seed=100,
    copy=False,
):
    """Function to find clusters with spagcn.

    Args:
        adata (class:`~anndata.AnnData`): an Anndata object, after normalization.
        n_clusters (int): Desired number of clusters.
        p (float, optional): parameter `p` in spagcn algorithm. See `SpaGCN` for details. Defaults to 0.5.
        s (int, optional): alpha to control the color scale in calculating adjacent matrix. Defaults to 1.
        b (int, optional): beta to control the range of neighbourhood when calculate grey value for one spot in calculating adjacent matrix. Defaults to 49.
        refine_shape (str, optional): Smooth the spatial domains with given spatial topology, "hexagon" for Visium data, "square" for ST data. Defaults to None.
        his_img_path (str, optional): The file path of histology image used to calculate adjacent matrix in spagcn algorithm. Defaults to None.
        total_umi (str, optional): By providing the key(colname) in `adata.obs` which contains total UMIs(counts) for each spot, the function use the total counts as
                                a grayscale image when histology image is not provided. Ignored if his_img_path is not `None`. Defaults to "total_umi".
        x_pixel (str, optional): The key(colname) in `adata.obs` which contains corresponding x-pixels in histology image. Defaults to "x_pixel".
        y_pixel (str, optional): The key(colname) in `adata.obs` which contains corresponding y-pixels in histology image. Defaults to "y_pixel".
        x_array (str, optional): The key(colname) in `adata.obs` which contains corresponding x-coordinates. Defaults to "x_array".
        y_array (str, optional): The key(colname) in `adata.obs` which contains corresponding y-coordinates. Defaults to "y_array".
        seed (int, optional): Global seed for `random`, `torch`, `numpy`. Defaults to 100.
        copy (bool): Whether to return a new deep copy of `adata` instead of updating `adata` object passed in arguments. Defaults to False.

    Returns:
        class:`~anndata.AnnData`: An `~anndata.AnnData` object with cluster info in "spagcn_pred", and in "spagcn_pred_refined" if `refine_shape` is set.
                                The adjacent matrix used in spagcn algorithm is saved in `adata.uns["adj_spagcn"]`.
    """

    x_array = adata.obs[x_array].tolist()
    y_array = adata.obs[y_array].tolist()
    x_pixel = adata.obs[x_pixel].tolist()
    y_pixel = adata.obs[y_pixel].tolist()

    s = 1
    b = 49

    if his_img_path is None:
        if total_umi is None:
            adj = calculate_adj_matrix(x=x_array, y=y_array, histology=False)
        else:
            total_umi = adata.obs[total_umi].tolist()
            total_umi = [x / max(total_umi) * 255 for x in total_umi]
            total_umi_mtx = pd.DataFrame({"x_pos": x_array, "y_pos": y_array, "n_umis": total_umi})
            total_umi_mtx = total_umi_mtx.pivot(index="x_pos", columns="y_pos", values="n_umis").fillna(0).to_numpy()
            umi_gs_img = np.dstack((total_umi_mtx, total_umi_mtx, total_umi_mtx))
            adj = calculate_adj_matrix(
                x=x_array,
                y=y_array,
                x_pixel=x_array,
                y_pixel=y_array,
                image=umi_gs_img,
                beta=b,
                alpha=s,
                histology=True,
            )
    else:
        img = cv2.imread(his_img_path)
        adj = calculate_adj_matrix(
            x=x_array,
            y=y_array,
            x_pixel=x_pixel,
            y_pixel=y_pixel,
            image=img,
            beta=b,
            alpha=s,
            histology=True,
        )

    adata.uns["adj_spagcn"] = adj

    l = search_l(p, adj, start=0.01, end=1000, tol=0.01, max_run=100)

    # Set seed
    r_seed = t_seed = n_seed = seed

    # Seaech for suitable resolution
    res = search_res(
        adata,
        adj,
        l,
        n_clusters,
        start=0.7,
        step=0.1,
        tol=5e-3,
        lr=0.05,
        max_epochs=20,
        r_seed=r_seed,
        t_seed=t_seed,
        n_seed=n_seed,
    )

    clf = SpaGCN()
    clf.set_l(l)

    # Set seed
    random.seed(r_seed)
    torch.manual_seed(t_seed)
    np.random.seed(n_seed)

    # Run
    clf.train(
        adata,
        adj,
        init_spa=True,
        init="louvain",
        res=res,
        tol=5e-3,
        lr=0.05,
        max_epochs=200,
    )
    y_pred, prob = clf.predict()
    adata.obs["spagcn_pred"] = y_pred
    adata.obs["spagcn_pred"] = adata.obs["spagcn_pred"].astype("category")

    if refine_shape is not None:
        # Do cluster refinement(optional)
        adj_2d = calculate_adj_matrix(x=x_array, y=y_array, histology=False)
        refined_pred = refine(
            sample_id=adata.obs.index.tolist(),
            pred=adata.obs["spagcn_pred"].tolist(),
            dis=adj_2d,
            shape=refine_shape,
        )
        adata.obs["spagcn_pred_refined"] = refined_pred
        adata.obs["spagcn_pred_refined"] = adata.obs["spagcn_pred_refined"].astype("category")

    if copy:
        return adata
    return None
