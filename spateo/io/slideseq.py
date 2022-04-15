"""IO functions for Slide-seq technology.
"""
from typing import List, Optional, Union

import ngs_tools as ngs
import numpy as np
import pandas as pd
from anndata import AnnData
from scipy.sparse import csr_matrix

from ..configuration import SKM
from ..logging import logger_manager as lm


def read_slideseq_as_dataframe(path: str) -> pd.DataFrame:
    """Read a Slide-seq digital expression matrix as long-format pandas DataFrame.

    Args:
        path: Path to file

    Return:
        Pandas DataFrame with the the following standardized column names.
            * `barcode`: Bead barcode
            * `gene`: Gene name/ID
            * `count`: Observed UMIs. Zeros are filtered.
    """
    df = pd.read_csv(path, sep="\t").rename(columns={"GENE": "gene"})
    df = df.melt(id_vars="gene", var_name="barcode", value_name="count")
    df = df[df["count"] > 0]
    df["gene"] = df["gene"].astype("category")
    df["barcode"] = df["barcode"].astype("category")
    df["count"] = df["count"].astype(np.uint16)
    return df


def read_slideseq_beads_as_dataframe(path: str) -> pd.DataFrame:
    """Read a Slide-seq bead locations file.

    Args:
        path: Path to file

    Return:
        Pandas DataFrame with the following columns.
            * `barcode`: Bead barcode
            * `x`, `y`: X, Y coordinates
    """
    skiprows = None
    with ngs.utils.open_as_text(path, "r") as f:
        line = f.readline()
        if line.startswith("barcode"):
            skiprows = 1
    df = pd.read_csv(path, skiprows=skiprows, names=["barcode", "x", "y"], dtype={"barcode": "category"})
    return df


def read_slideseq(
    path: str,
    beads_path: str,
    scale: float = 1.0,
    scale_unit: Optional[str] = None,
    binsize: Optional[int] = None,
) -> AnnData:
    """Read Slide-seq data as AnnData.

    Args:
        path: Path to Slide-seq digital expression matrix CSV.
        beads_path: Path to CSV file containing bead locations.
        scale: Physical length per coordinate. For visualization only.
        scale_unit: Scale unit.
        binsize: Size of pixel bins.
    """
    data = read_slideseq_as_dataframe(path)
    beads = read_slideseq_beads_as_dataframe(beads_path)
    data = pd.merge(data, beads, on="barcode")

    if binsize is not None:
        lm.main_info(f"Using binsize={binsize}")
        x_bin = bin_indices(data["x"].values, 0, binsize)
        y_bin = bin_indices(data["y"].values, 0, binsize)
        data["x"], data["y"] = x_bin, y_bin

        data["label"] = data["x"].astype(str) + "-" + data["y"].astype(str)
        props = get_bin_props(data[["x", "y", "label"]].drop_duplicates(), binsize)
    else:
        data.rename(columns={"barcode": "label"}, inplace=True)
        props = (
            data[["x", "y", "label"]]
            .drop_duplicates()
            .set_index("label")
            .rename({"x": "centroid-0", "y": "centroid-1"})
        )

    uniq_gene = sorted(data["gene"].unique())
    uniq_cell = sorted(data["label"].unique())
    shape = (len(uniq_cell), len(uniq_gene))
    cell_dict = dict(zip(uniq_cell, range(len(uniq_cell))))
    gene_dict = dict(zip(uniq_gene, range(len(uniq_gene))))

    x_ind = data["label"].map(cell_dict).astype(int).values
    y_ind = data["gene"].map(gene_dict).astype(int).values

    lm.main_info("Constructing count matrix.")
    X = csr_matrix((data["count"].values, (x_ind, y_ind)), shape=shape)
    obs = pd.DataFrame(index=uniq_cell)
    var = pd.DataFrame(index=uniq_gene)
    adata = AnnData(X=X, obs=obs, var=var)
    ordered_props = props.loc[adata.obs_names]
    adata.obsm["spatial"] = ordered_props.filter(regex="centroid-").values

    # Set uns
    SKM.init_adata_type(adata, SKM.ADATA_UMI_TYPE)
    SKM.init_uns_pp_namespace(adata)
    SKM.init_uns_spatial_namespace(adata)
    SKM.set_uns_spatial_attribute(adata, SKM.UNS_SPATIAL_BINSIZE_KEY, binsize)
    SKM.set_uns_spatial_attribute(adata, SKM.UNS_SPATIAL_SCALE_KEY, scale)
    SKM.set_uns_spatial_attribute(adata, SKM.UNS_SPATIAL_SCALE_UNIT_KEY, scale_unit)
    return adata