"""
Modeling cell-cell communication using a regression model that is considerate of the spatial heterogeneity of (and thus
the context-dependency of the relationships of) the response variable.
"""
import argparse
import math
import os
import re
import sys
from copy import deepcopy
from functools import partial
from itertools import product
from multiprocessing import Pool
from typing import Callable, Dict, List, Optional, Tuple, Union

import anndata
import numpy as np
import pandas as pd
import scipy
from mpi4py import MPI
from scipy import special
from scipy.spatial.distance import cdist
from sklearn.cluster import KMeans

# For now, add Spateo working directory to sys path so compiler doesn't look in the installed packages:
sys.path.insert(0, "/mnt/c/Users/danie/Desktop/Github/Github/spateo-release-main")

from spateo.logging import logger_manager as lm
from spateo.preprocessing.normalize import normalize_total
from spateo.preprocessing.transform import log1p
from spateo.tools.find_neighbors import get_wi, transcriptomic_connectivity
from spateo.tools.spatial_degs import moran_i
from spateo.tools.ST_regression.distributions import Gaussian, NegativeBinomial, Poisson
from spateo.tools.ST_regression.regression_utils import (
    compute_betas_local,
    iwls,
    multicollinearity_check,
    smooth,
)

# NOTE: set lower bound AND upper bound bandwidth much lower for membrane-bound ligands/receptors pairs

# ---------------------------------------------------------------------------------------------------
# GWR for cell-cell communication
# ---------------------------------------------------------------------------------------------------
class SWR:
    """Spatially weighted regression on spatial omics data with parallel processing. Runs after being called
    from the command line.

    Args:
        comm: MPI communicator object initialized with mpi4py, to control parallel processing operations
        parser: ArgumentParser object initialized with argparse, to parse command line arguments for arguments
            pertinent to modeling.

    Attributes:
        mod_type: The type of model that will be employed- this dictates how the data will be processed and
            prepared. Options:
                - "niche": Spatially-aware, uses spatial connections between samples as independent variables
                - "lr": Spatially-aware, uses the combination of receptor expression in the "target" cell and spatially
                    lagged ligand expression in the neighboring cells as independent variables.
                - "slice": Spatially-aware, uses a coupling of spatial category connections, ligand expression
                    and receptor expression to perform regression on select receptor-downstream genes.


        adata_path: Path to the AnnData object from which to extract data for modeling
        csv_path: Can also be used to specify path to non-AnnData .csv object. Assumes the first three columns
            contain x- and y-coordinates and then dependent variable values, in that order, with all subsequent
            columns containing independent variable values.
        normalize: Set True to Perform library size normalization, to set total counts in each cell to the same
            number (adjust for cell size). It is advisable not to do this if performing Poisson or negative binomial
            regression.
        smooth: Set True to correct for dropout effects by leveraging gene expression neighborhoods to smooth
            expression. It is advisable not to do this if performing Poisson or negative binomial regression.
        log_transform: Set True if log-transformation should be applied to expression. It is advisable not to do
            this if performing Poisson or negative binomial regression.
        target_expr_threshold: Only used if :param `mod_type` is "lr" or "slice" and :param `targets_path` is not
            given. When manually selecting targets, expression above a threshold percentage of cells will be used to
            filter to a smaller subset of interesting genes. Defaults to 0.2.


        custom_lig_path: Optional path to a .txt file containing a list of ligands for the model, separated by
            newlines. Only used if :attr `mod_type` is "lr" or "slice" (and thus uses ligand/receptor expression
            directly in the inference). If not provided, will select ligands using a threshold based on expression
            levels in the data.
        custom_rec_path: Optional path to a .txt file containing a list of receptors for the model, separated by
            newlines. Only used if :attr `mod_type` is "lr" or "slice" (and thus uses ligand/receptor expression
            directly in the inference). If not provided, will select receptors using a threshold based on expression
            levels in the data.
        custom_pathways_path: Rather than  providing a list of receptors, can provide a list of signaling pathways-
            all receptors with annotations in this pathway will be included in the model. Only used if :attr `mod_type`
            is "lr" or "slice".
        targets_path: Optional path to a .txt file containing a list of prediction target genes for the model,
            separated by newlines. If not provided, targets will be strategically selected from the given receptors.
        init_betas_path: Optional path to a .npy file containing initial coefficient values for the model. Initial
            coefficients should have shape [n_features, ].


        cci_dir: Full path to the directory containing cell-cell communication databases
        species: Selects the cell-cell communication database the relevant ligands will be drawn from. Options:
                "human", "mouse".
        output_path: Full path name for the .csv file in which results will be saved


        coords_key: Key in .obsm of the AnnData object that contains the coordinates of the cells
        group_key: Key in .obs of the AnnData object that contains the category grouping for each cell
        covariate_keys: Can be used to optionally provide any number of keys in .obs or .var containing a continuous
            covariate (e.g. expression of a particular TF, avg. distance from a perturbed cell, etc.)


        bw: Used to provide previously obtained bandwidth for the spatial kernel. Consists of either a distance
            value or N for the number of nearest neighbors. Can be obtained using BW_Selector or some other
            user-defined method. Pass "np.inf" if all other points should have the same spatial weight. Defaults to
            1000 if not provided.
        minbw: For use in automated bandwidth selection- the lower-bound bandwidth to test.
        maxbw: For use in automated bandwidth selection- the upper-bound bandwidth to test.


        distr: Distribution family for the dependent variable; one of "gaussian", "poisson", "nb"
        kernel: Type of kernel function used to weight observations; one of "bisquare", "exponential", "gaussian",
            "quadratic", "triangular" or "uniform".


        bw_fixed: Set True for distance-based kernel function and False for nearest neighbor-based kernel function
        exclude_self: If True, ignore each sample itself when computing the kernel density estimation
        fit_intercept: Set True to include intercept in the model and False to exclude intercept
    """

    def __init__(self, comm: MPI.Comm, parser: argparse.ArgumentParser):
        self.logger = lm.get_main_logger()

        self.comm = comm
        self.parser = parser

        self.mod_type = None
        self.species = None
        self.ligands = None
        self.receptors = None
        self.targets = None
        self.normalize = None
        self.smooth = None
        self.log_transform = None
        self.target_expr_threshold = None

        self.coords = None
        self.groups = None
        self.y = None
        self.X = None

        self.bw = None
        self.minbw = None
        self.maxbw = None

        self.distr = None
        self.kernel = None
        # Number of samples, equal to the number of SWR runs to go through:
        self.n_samples = None
        self.n_features = None
        # Flag for whether model has been set up and AnnData has been processed:
        self.set_up = False

        self.parse_stgwr_args()

    def _set_up_model(self):
        if self.mod_type is None and self.adata_path is not None:
            raise ValueError(
                "No model type provided; need to provide a model type to fit. Options: 'niche', 'lr', " "'slice'."
            )

        # Check if the program is currently in the master process:
        if self.comm.rank == 0:
            # If AnnData object is given, process it:
            if self.adata_path is not None:
                # Ensure CCI directory is provided:
                if self.cci_dir is None:
                    raise ValueError(
                        "No CCI directory provided; need to provide a CCI directory to fit a model with "
                        "ligand/receptor expression."
                    )
                self.load_and_process()
            else:
                if self.csv_path is None:
                    raise ValueError(
                        "No AnnData path or .csv path provided; need to provide at least one of these "
                        "to provide a default dataset to fit."
                    )
                else:
                    custom_data = pd.read_csv(self.csv_path, index_col=0)
                    self.coords = custom_data.iloc[:, :2].values
                    self.target = pd.DataFrame(
                        custom_data.iloc[:, 2], index=custom_data.index, columns=[custom_data.columns[2]]
                    )
                    self.logger.info(f"Extracting target from column labeled '{custom_data.columns[2]}'.")
                    independent_variables = custom_data.iloc[:, 3:]
                    self.X = independent_variables.values
                    self.feature_names = list(independent_variables.columns)

                    # Add intercept if applicable:
                    if self.fit_intercept:
                        self.X = np.concatenate((np.ones((self.X.shape[0], 1)), self.X), axis=1)
                        self.feature_names = ["intercept"] + self.feature_names

                    self.n_samples = self.X.shape[0]
                    self.n_features = self.X.shape[1]
                    self.sample_names = custom_data.index

                    # Subsample if applicable:
                    if self.subsample:
                        self.run_subsample()

        # Broadcast data to other processes- gene expression variables:
        if self.adata_path is not None:
            if self.mod_type == "niche" or self.mod_type == "slice":
                self.cell_categories = self.comm.bcast(self.cell_categories, root=0)
            if self.mod_type == "lr" or self.mod_type == "slice":
                self.ligands_expr = self.comm.bcast(self.ligands_expr, root=0)
                self.receptors_expr = self.comm.bcast(self.receptors_expr, root=0)
            if hasattr(self, "targets_expr"):
                self.targets_expr = self.comm.bcast(self.targets_expr, root=0)
            elif hasattr(self, "target"):
                self.target = self.comm.bcast(self.target, root=0)

        # Broadcast data to other processes:
        self.X = self.comm.bcast(self.X, root=0)
        self.bw = self.comm.bcast(self.bw, root=0)
        self.coords = self.comm.bcast(self.coords, root=0)
        self.tolerance = self.comm.bcast(self.tolerance, root=0)
        self.max_iter = self.comm.bcast(self.max_iter, root=0)
        self.alpha = self.comm.bcast(self.alpha, root=0)
        self.n_samples = self.comm.bcast(self.n_samples, root=0)
        self.n_features = self.comm.bcast(self.n_features, root=0)

        # Split data into chunks for each process:
        if self.subsample:
            self.run_subsample()
        else:
            chunk_size = int(math.ceil(float(len(self.n_samples)) / self.comm.size))
            # Assign chunks to each process:
            self.x_chunk = self.n_samples[self.comm.rank * chunk_size : (self.comm.rank + 1) * chunk_size]

        # Indicate model has now been set up:
        self.set_up = True

    def parse_stgwr_args(self):
        """
        Parse command line arguments for arguments pertinent to modeling.
        """
        self.arg_retrieve = self.parser.parse_args()
        self.mod_type = self.arg_retrieve.mod_type
        # GRN inherits from this class and has slightly different preprocessing options that can be accessed using
        # its own flag:
        self.grn = self.arg_retrieve.grn
        # Set flag to evenly subsample spatial data:
        self.subsample = self.arg_retrieve.subsample

        self.adata_path = self.arg_retrieve.adata_path
        self.csv_path = self.arg_retrieve.csv_path
        self.cci_dir = self.arg_retrieve.cci_dir
        self.species = self.arg_retrieve.species
        self.output_path = self.arg_retrieve.output_path
        self.custom_ligands_path = self.arg_retrieve.custom_lig_path
        self.custom_receptors_path = self.arg_retrieve.custom_rec_path
        self.custom_pathways_path = self.arg_retrieve.custom_pathways_path
        self.targets_path = self.arg_retrieve.targets_path
        self.init_betas_path = self.arg_retrieve.init_betas_path
        # Check if path to init betas is given:
        if self.init_betas_path is not None:
            self.logger.info(f"Loading initial betas from: {self.init_betas_path}")
            self.init_betas = np.load(self.init_betas_path)
        else:
            self.init_betas = None

        self.normalize = self.arg_retrieve.normalize
        self.smooth = self.arg_retrieve.smooth
        self.log_transform = self.arg_retrieve.log_transform
        self.target_expr_threshold = self.arg_retrieve.target_expr_threshold
        self.multicollinear_threshold = self.arg_retrieve.multicollinear_threshold

        self.coords_key = self.arg_retrieve.coords_key
        self.group_key = self.arg_retrieve.group_key
        self.group_subset = self.arg_retrieve.group_subset
        self.covariate_keys = self.arg_retrieve.covariate_keys

        self.multiscale_flag = self.arg_retrieve.multiscale
        self.multiscale_params_only = self.arg_retrieve.multiscale_params_only
        self.bw_fixed = self.arg_retrieve.bw_fixed
        self.exclude_self = self.arg_retrieve.exclude_self
        self.distr = self.arg_retrieve.distr
        # Get appropriate distribution family based on specified:
        if self.distr == "gaussian":
            link = Gaussian.__init__.__defaults__[0]
            self.distr_obj = Gaussian(link)
        elif self.distr == "poisson":
            link = Poisson.__init__.__defaults__[0]
            self.distr_obj = Poisson(link)
        elif self.distr == "nb":
            link = NegativeBinomial.__init__.__defaults__[0]
            self.distr_obj = NegativeBinomial(link)
        self.kernel = self.arg_retrieve.kernel

        if not self.bw_fixed and self.kernel not in ["bisquare", "uniform"]:
            raise ValueError(
                "`bw_fixed` is set to False for adaptive kernel- it is assumed the chosen bandwidth is "
                "the number of neighbors for each sample. However, only the `bisquare` and `uniform` "
                "kernels perform hard thresholding and so it is recommended to use one of these kernels- "
                "the other kernels may result in different results."
            )

        self.fit_intercept = self.arg_retrieve.fit_intercept
        # Parameters related to the fitting process (tolerance, number of iterations, etc.)
        self.tolerance = self.arg_retrieve.tolerance
        self.max_iter = self.arg_retrieve.max_iter
        self.patience = self.arg_retrieve.patience
        self.alpha = self.arg_retrieve.alpha
        self.multiscale_chunks = self.arg_retrieve.chunks

        if self.arg_retrieve.bw:
            if self.bw_fixed:
                self.bw = float(self.arg_retrieve.bw)
            else:
                self.bw = int(self.arg_retrieve.bw)

        if self.arg_retrieve.minbw:
            if self.bw_fixed:
                self.minbw = float(self.arg_retrieve.minbw)
            else:
                self.minbw = int(self.arg_retrieve.minbw)

        if self.arg_retrieve.maxbw:
            if self.bw_fixed:
                self.maxbw = float(self.arg_retrieve.maxbw)
            else:
                self.maxbw = int(self.arg_retrieve.maxbw)

        # Helpful messages at process start:
        if self.comm.rank == 0:
            print("-" * 60, flush=True)
            self.logger.info(f"Running SWR on {self.comm.size} processes...")
            fixed_or_adaptive = "Fixed " if self.bw_fixed else "Adaptive "
            type = fixed_or_adaptive + self.kernel.capitalize()
            self.logger.info(f"Spatial kernel: {type}")

            if self.adata_path is not None:
                self.logger.info(f"Loading AnnData object from: {self.adata_path}")
            elif self.csv_path is not None:
                self.logger.info(f"Loading CSV file from: {self.csv_path}")
            if self.mod_type is not None:
                self.logger.info(f"Model type: {self.mod_type}")
                self.logger.info(f"Loading cell-cell interaction databases from the following folder: {self.cci_dir}")
                if self.custom_ligands_path is not None:
                    self.logger.info(f"Using list of custom ligands from: {self.custom_ligands_path}")
                if self.custom_receptors_path is not None:
                    self.logger.info(f"Using list of custom receptors from: {self.custom_receptors_path}")
                if self.targets_path is not None:
                    self.logger.info(f"Using list of target genes from: {self.targets_path}")
                self.logger.info(
                    f"Saving results to: {self.output_path}. Note that running `fit` or "
                    f"`predict_and_save` will clear the contents of this folder- copy any essential "
                    f"files beforehand."
                )

    def load_and_process(self):
        """
        Load AnnData object and process it for modeling.
        """
        self.adata = anndata.read_h5ad(self.adata_path)
        self.adata.uns["__type"] = "UMI"
        self.sample_names = self.adata.obs_names
        self.coords = self.adata.obsm[self.coords_key]
        self.n_samples = self.adata.n_obs
        # Placeholder- this will change at time of fitting:
        self.n_features = self.adata.n_vars

        if self.distr in ["poisson", "nb"]:
            if self.normalize or self.smooth or self.log_transform:
                self.logger.info(
                    f"With a {self.distr} assumption, discrete counts are required for the response variable. "
                    f"Computing normalizations and transforms if applicable, but rounding nonintegers up to nearest "
                    f"integer; original counts can be round in .layers['raw']. Log-transform should not be applied."
                )
                self.adata.layers["raw"] = self.adata.X

        if self.normalize:
            if self.distr == "gaussian":
                self.logger.info("Setting total counts in each cell to 1e4 inplace...")
                normalize_total(self.adata)
            else:
                self.logger.info("Setting total counts in each cell to 1e4 and rounding nonintegers inplace...")
                normalize_total(self.adata)
                self.adata.X = (
                    scipy.sparse.csr_matrix(np.round(self.adata.X))
                    if scipy.sparse.issparse(self.adata.X)
                    else np.round(self.adata.X)
                )

        # Smooth data if 'smooth' is True and log-transform data matrix if 'log_transform' is True:
        if self.smooth:
            # Compute connectivity matrix if not already existing:
            try:
                conn = self.adata.obsp["expression_connectivities"]
            except:
                _, adata = transcriptomic_connectivity(self.adata, n_neighbors_method="ball_tree")
                conn = adata.obsp["expression_connectivities"]

            if self.distr == "gaussian":
                self.logger.info("Smoothing gene expression inplace...")
                adata_smooth_norm, _ = smooth(self.adata.X, conn, normalize_W=True)
                self.adata.X = adata_smooth_norm

            else:
                self.logger.info("Smoothing gene expression and rounding nonintegers inplace...")
                adata_smooth_norm, _ = smooth(self.adata.X, conn, normalize_W=True, return_discrete=True)
                self.adata.X = adata_smooth_norm

        if self.log_transform:
            if self.distr == "gaussian":
                self.logger.info("Log-transforming expression inplace...")
                self.adata.X = log1p(self.adata)
            else:
                self.logger.info(
                    "For the chosen distributional assumption, log-transform should not be applied. Log-transforming "
                    "expression and storing in adata.layers['X_log1p'], but not applying inplace and not using for "
                    "modeling."
                )
                self.adata.layers["X_log1p"] = log1p(self.adata)

        # Construct initial arrays for CCI modeling:
        self.define_sig_inputs()

    def define_sig_inputs(self, adata: Optional[anndata.AnnData] = None):
        """For signaling-relevant models, define necessary quantities that will later be used to define the independent
        variable array- the one-hot cell-type array, the ligand expression array and the receptor expression array."""
        if adata is None:
            adata = self.adata.copy()

        # One-hot cell type array (or other category):
        if self.mod_type == "niche" or self.mod_type == "slice":
            group_name = adata.obs[self.group_key]
            db = pd.DataFrame({"group": group_name})
            categories = np.array(group_name.unique().tolist())
            db["group"] = pd.Categorical(db["group"], categories=categories)

            self.logger.info("Preparing data: converting categories to one-hot labels for all samples.")
            X = pd.get_dummies(data=db, drop_first=False)
            # Ensure columns are in order:
            self.cell_categories = X.reindex(sorted(X.columns), axis=1)
            # Ensure each category is one word with no spaces or special characters:
            self.cell_categories.columns = [
                re.sub(r"\b([a-zA-Z0-9])", lambda match: match.group(1).upper(), re.sub(r"[^a-zA-Z0-9]+", "", s))
                for s in self.cell_categories.columns
            ]

        # Ligand-receptor expression array
        if self.mod_type == "lr" or self.mod_type == "slice":
            if self.species == "human":
                self.lr_db = pd.read_csv(os.path.join(self.cci_dir, "lr_db_human.csv"), index_col=0)
                r_tf_db = pd.read_csv(os.path.join(self.cci_dir, "human_receptor_TF_db.csv"), index_col=0)
                tf_target_db = pd.read_csv(os.path.join(self.cci_dir, "human_TF_target_db.csv"), index_col=0)
            elif self.species == "mouse":
                self.lr_db = pd.read_csv(os.path.join(self.cci_dir, "lr_db_mouse.csv"), index_col=0)
                r_tf_db = pd.read_csv(os.path.join(self.cci_dir, "mouse_receptor_TF_db.csv"), index_col=0)
                tf_target_db = pd.read_csv(os.path.join(self.cci_dir, "mouse_TF_target_db.csv"), index_col=0)
            else:
                raise ValueError("Invalid species specified. Must be one of 'human' or 'mouse'.")
            database_ligands = set(self.lr_db["from"])
            database_receptors = set(self.lr_db["to"])
            database_pathways = set(r_tf_db["pathway"])

            if self.custom_ligands_path is not None:
                with open(self.custom_ligands_path, "r") as f:
                    ligands = f.read().splitlines()
                    ligands = [l for l in ligands if l in database_ligands]
                    l_complexes = [elem for elem in ligands if "_" in elem]
                    # Get individual components if any complexes are included in this list:
                    ligands = [l for item in ligands for l in item.split("_")]
            else:
                # List of possible complexes to search through:
                l_complexes = [elem for elem in database_ligands if "_" in elem]
                # And all possible ligand molecules:
                all_ligands = [l for item in database_ligands for l in item.split("_")]

                # Get list of ligands from among the most highly spatially-variable genes, indicative of potentially
                # interesting spatially-enriched signal:
                self.logger.info(
                    "Preparing data: getting list of ligands from among the most highly " "spatially-variable genes."
                )
                m_degs = moran_i(adata)
                m_filter_genes = m_degs[m_degs.moran_q_val < 0.05].sort_values(by=["moran_i"], ascending=False).index
                ligands = [g for g in m_filter_genes if g in all_ligands]

                # If no significant spatially-variable ligands are found, use the top 10 most spatially-variable
                # ligands:
                if len(ligands) == 0:
                    self.logger.info(
                        "No significant spatially-variable ligands found. Using top 10 most "
                        "spatially-variable ligands."
                    )
                    m_filter_genes = m_degs.sort_values(by=["moran_i"], ascending=False).index
                    ligands = [g for g in m_filter_genes if g in all_ligands][:10]

                # If any ligands are part of complexes, add all complex components to this list:
                for element in l_complexes:
                    if "_" in element:
                        complex_members = element.split("_")
                        for member in complex_members:
                            if member in ligands:
                                other_members = [m for m in complex_members if m != member]
                                for member in other_members:
                                    ligands.append(member)
                ligands = list(set(ligands))

                self.logger.info(
                    f"Found {len(ligands)} among significantly spatially-variable genes and associated "
                    f"complex members."
                )

            ligands = [l for l in ligands if l in adata.var_names]
            self.ligands_expr = pd.DataFrame(
                adata[:, ligands].X.toarray() if scipy.sparse.issparse(adata.X) else adata[:, ligands].X,
                index=adata.obs_names,
                columns=ligands,
            )
            # Combine columns if they are part of a complex- eventually the individual columns should be dropped,
            # but store them in a temporary list to do so later because some may contribute to multiple complexes:
            to_drop = []
            for element in l_complexes:
                parts = element.split("_")
                if all(part in self.ligands_expr.columns for part in parts):
                    # Combine the columns into a new column with the name of the hyphenated element- here we will
                    # compute the geometric mean of the expression values of the complex components:
                    self.ligands_expr[element] = self.ligands_expr[parts].apply(
                        lambda x: x.prod() ** (1 / len(parts)), axis=1
                    )
                    # Mark the individual components for removal if the individual components cannot also be
                    # found as ligands:
                    to_drop.extend([part for part in parts if part not in database_ligands])
                else:
                    # Drop the hyphenated element from the dataframe if all components are not found in the
                    # dataframe columns
                    partial_components = [l for l in ligands if l in parts]
                    to_drop.extend(partial_components)
                    if len(partial_components) > 0:
                        self.logger.info(
                            f"Not all components from the {element} heterocomplex could be found in the " f"dataset."
                        )

            # Drop any possible duplicate ligands alongside any other columns to be dropped:
            to_drop = list(set(to_drop))
            self.ligands_expr.drop(to_drop, axis=1, inplace=True)
            first_occurrences = self.ligands_expr.columns.duplicated(keep="first")
            self.ligands_expr = self.ligands_expr.loc[:, ~first_occurrences]

            if self.custom_receptors_path is not None:
                with open(self.custom_receptors_path, "r") as f:
                    receptors = f.read().splitlines()
                    receptors = [r for r in receptors if r in database_receptors]
                    r_complexes = [elem for elem in receptors if "_" in elem]
                    # Get individual components if any complexes are included in this list:
                    receptors = [r for item in receptors for r in item.split("_")]

            elif self.custom_pathways_path is not None:
                with open(self.custom_pathways_path, "r") as f:
                    pathways = f.read().splitlines()
                    pathways = [p for p in pathways if p in database_pathways]
                # Get all receptors associated with these pathway(s):
                r_tf_db_subset = r_tf_db[r_tf_db["pathway"].isin(pathways)]
                receptors = set(r_tf_db_subset["receptor"])
                r_complexes = [elem for elem in receptors if "_" in elem]
                # Get individual components if any complexes are included in this list:
                receptors = [r for item in receptors for r in item.split("_")]
                receptors = list(set(receptors))

            else:
                # List of possible complexes to search through:
                r_complexes = [elem for elem in database_receptors if "_" in elem]
                # And all possible receptor molecules:
                all_receptors = [r for item in database_receptors for r in item.split("_")]

                # Get list of receptors from among the most highly spatially-variable genes, indicative of
                # potentially interesting spatially-enriched signal:
                self.logger.info(
                    "Preparing data: getting list of ligands from among the most highly spatially-variable genes."
                )
                m_degs = moran_i(adata)
                m_filter_genes = m_degs[m_degs.moran_q_val < 0.05].sort_values(by=["moran_i"], ascending=False).index
                receptors = [g for g in m_filter_genes if g in all_receptors]

                # If no significant spatially-variable receptors are found, use the top 10 most spatially-variable
                # receptors:
                if len(receptors) == 0:
                    self.logger.info(
                        "No significant spatially-variable receptors found. Using top 10 most "
                        "spatially-variable receptors."
                    )
                    m_filter_genes = m_degs.sort_values(by=["moran_i"], ascending=False).index
                    receptors = [g for g in m_filter_genes if g in all_receptors][:10]

                # If any receptors are part of complexes, add all complex components to this list:
                for element in r_complexes:
                    if "_" in element:
                        complex_members = element.split("_")
                        for member in complex_members:
                            if member in receptors:
                                other_members = [m for m in complex_members if m != member]
                                for member in other_members:
                                    receptors.append(member)
                receptors = list(set(receptors))

                self.logger.info(
                    f"Found {len(receptors)} among significantly spatially-variable genes and associated "
                    f"complex members."
                )

            receptors = [r for r in receptors if r in adata.var_names]

            self.receptors_expr = pd.DataFrame(
                adata[:, receptors].X.toarray() if scipy.sparse.issparse(adata.X) else adata[:, receptors].X,
                index=adata.obs_names,
                columns=receptors,
            )

            # Combine columns if they are part of a complex- eventually the individual columns should be dropped,
            # but store them in a temporary list to do so later because some may contribute to multiple complexes:
            to_drop = []
            for element in r_complexes:
                if "_" in element:
                    parts = element.split("_")
                    if all(part in self.receptors_expr.columns for part in parts):
                        # Combine the columns into a new column with the name of the hyphenated element- here we will
                        # compute the geometric mean of the expression values of the complex components:
                        self.receptors_expr[element] = self.receptors_expr[parts].apply(
                            lambda x: x.prod() ** (1 / len(parts)), axis=1
                        )
                        # Mark the individual components for removal if the individual components cannot also be
                        # found as receptors:
                        to_drop.extend([part for part in parts if part not in database_receptors])
                    else:
                        # Drop the hyphenated element from the dataframe if all components are not found in the
                        # dataframe columns
                        partial_components = [r for r in receptors if r in parts]
                        to_drop.extend(partial_components)
                        if len(partial_components) > 0:
                            self.logger.info(
                                f"Not all components from the {element} heterocomplex could be found in the "
                                f"dataset, so this complex was not included."
                            )

            # Drop any possible duplicate ligands alongside any other columns to be dropped:
            to_drop = list(set(to_drop))
            self.receptors_expr.drop(to_drop, axis=1, inplace=True)
            first_occurrences = self.receptors_expr.columns.duplicated(keep="first")
            self.receptors_expr = self.receptors_expr.loc[:, ~first_occurrences]

            # Ensure there is some degree of compatibility between the selected ligands and receptors:
            self.logger.info("Preparing data: finding matched pairs between the selected ligands and receptors.")
            starting_n_ligands = len(self.ligands_expr.columns)
            starting_n_receptors = len(self.receptors_expr.columns)

            lr_ref = self.lr_db[["from", "to"]]
            # Don't need entire dataframe, just take the first two rows of each:
            lig_melt = self.ligands_expr.iloc[[0, 1], :].melt(var_name="from", value_name="value_ligand")
            rec_melt = self.receptors_expr.iloc[[0, 1], :].melt(var_name="to", value_name="value_receptor")

            merged_df = pd.merge(lr_ref, rec_melt, on="to")
            merged_df = pd.merge(merged_df, lig_melt, on="from")
            pairs = merged_df[["from", "to"]].drop_duplicates(keep="first")
            self.lr_pairs = [tuple(x) for x in zip(pairs["from"], pairs["to"])]
            if len(self.lr_pairs) == 0:
                raise RuntimeError(
                    "No matched pairs between the selected ligands and receptors were found. If path to custom list of "
                    "ligands and/or receptors was provided, ensure ligand-receptor pairings exist among these lists, "
                    "or check data to make sure these ligands and/or receptors were measured and were not filtered out."
                )

            pivoted_df = merged_df.pivot_table(values=["value_ligand", "value_receptor"], index=["from", "to"])
            filtered_df = pivoted_df[pivoted_df.notna().all(axis=1)]
            # Filter ligand and receptor expression to those that have a matched pair:
            self.ligands_expr = self.ligands_expr[filtered_df.index.get_level_values("from").unique()]
            self.receptors_expr = self.receptors_expr[filtered_df.index.get_level_values("to").unique()]
            final_n_ligands = len(self.ligands_expr.columns)
            final_n_receptors = len(self.receptors_expr.columns)

            self.logger.info(
                f"Found {final_n_ligands} ligands and {final_n_receptors} receptors that have matched pairs. "
                f"{starting_n_ligands - final_n_ligands} ligands removed from the list and "
                f"{starting_n_receptors - final_n_receptors} receptors/complexes removed from the list due to not "
                f"having matched pairs among the corresponding set of receptors/ligands, respectively."
                f"Remaining ligands: {self.ligands_expr.columns.tolist()}."
                f"Remaining receptors: {self.receptors_expr.columns.tolist()}."
            )

            self.logger.info(f"Set of ligand-receptor pairs: {self.lr_pairs}")

        else:
            raise ValueError("Invalid `mod_type` specified. Must be one of 'niche', 'slice', or 'lr'.")

        # Get gene targets:
        self.logger.info("Preparing data: getting gene targets.")
        # For niche model, targets must be manually provided:
        if self.targets_path is None and self.mod_type == "niche":
            raise ValueError(
                "For niche model, `targets_path` must be provided. For slice and L:R models, targets can be "
                "automatically inferred, but ligand/receptor information does not exist for the niche model."
            )

        if self.targets_path is not None:
            with open(self.targets_path, "r") as f:
                targets = f.read().splitlines()
                targets = [t for t in targets if t in adata.var_names]

        # Else get targets by connecting to the targets of the L:R-downstream transcription factors:
        else:
            # Get the targets of the L:R-downstream transcription factors:
            tf_subset = r_tf_db[r_tf_db["receptor"].isin(self.receptors_expr.columns)]
            tfs = set(tf_subset["tf"])
            tfs = [tf for tf in tfs if tf in adata.var_names]
            # Subset to TFs that are expressed in > threshold number of cells:
            if scipy.sparse.issparse(adata.X):
                tf_expr_percentage = np.array((adata[:, tfs].X > 0).sum(axis=0) / adata.n_obs)[0]
            else:
                tf_expr_percentage = np.count_nonzero(adata[:, tfs].X, axis=0) / adata.n_obs
            tfs = np.array(tfs)[tf_expr_percentage > self.target_expr_threshold]

            targets_subset = tf_target_db[tf_target_db["TF"].isin(tfs)]
            targets = list(set(targets_subset["target"]))
            targets = [target for target in targets if target in adata.var_names]
            # Subset to targets that are expressed in > threshold number of cells:
            if scipy.sparse.issparse(adata.X):
                target_expr_percentage = np.array((adata[:, targets].X > 0).sum(axis=0) / adata.n_obs)[0]
            else:
                target_expr_percentage = np.count_nonzero(adata[:, targets].X, axis=0) / adata.n_obs
            targets = np.array(targets)[target_expr_percentage > self.target_expr_threshold]

        self.targets_expr = pd.DataFrame(
            adata[:, targets].X.toarray() if scipy.sparse.issparse(adata.X) else adata[:, targets].X,
            index=adata.obs_names,
            columns=targets,
        )

        # Compute initial spatial weights for all samples- use twice the min distance as initial bandwidth if not
        # provided (for fixed bw) or 10 nearest neighbors (for adaptive bw):
        if self.bw is None:
            if self.bw_fixed:
                init_bw = (
                    np.min(
                        np.array(
                            [np.min(np.delete(cdist([self.coords[i]], self.coords), 0)) for i in range(self.n_samples)]
                        )
                    )
                    * 2
                )
            else:
                init_bw = 10
        else:
            init_bw = self.bw
        self.all_spatial_weights = self._compute_all_wi(init_bw)
        self.all_spatial_weights = self.comm.bcast(self.all_spatial_weights, root=0)

    def run_subsample(self, y: Optional[pd.DataFrame] = None):
        """To combat computational intensiveness of this regressive protocol, subsampling will be performed in cases
        where there are >= 5000 cells or in cases where specific cell types are manually selected for fitting- local
        fit will be performed only on this subset under the assumption that discovered signals will not be
        significantly different for the subsampled data.

        New Attributes:
            indices: Dictionary containing indices of the subsampled cells for each dependent variable
            n_samples_fitted: Dictionary containing number of samples to be fit (not total number of samples) for
                each dependent variable
            sample_names: Dictionary containing lists of names of the subsampled cells for each dependent variable
            n_runs_all: Dictionary containing the number of runs for each dependent variable
        """
        # Dictionary to store both cell labels (:attr `subsampled_sample_names`) and numerical indices (:attr
        # `indices`) of subsampled points, :attr `n_samples_fitted` (for setting :attr `x_chunk` later on, and
        # :attr `neighboring_unsampled` to establish a mapping between each not-sampled point and the closest sampled
        # point:
        self.indices, self.n_samples_fitted, self.subsampled_sample_names, self.neighboring_unsampled = {}, {}, {}, {}

        if y is None:
            y_arr = self.targets_expr if hasattr(self, "targets_expr") else self.target
        else:
            y_arr = y

        # Also include option to subsample particular cell types of interest:

        for target in y_arr.columns:
            # Spatial clustering:
            n_clust = int(0.05 * self.n_samples)
            kmeans = KMeans(n_clusters=n_clust, random_state=0).fit(self.coords)
            if hasattr(self, "adata"):
                self.adata.obs["spatial_cluster"] = kmeans.predict(self.coords).astype(int)
                spatial_clusters = self.adata.obs["spatial_cluster"].values.reshape(-1, 1)
            else:
                spatial_clusters = kmeans.predict(self.coords).astype(int).reshape(-1, 1)

            data = np.concatenate(
                (
                    self.coords,
                    spatial_clusters,
                    y_arr[target].values.reshape(-1, 1),
                ),
                axis=1,
            )
            temp_df = pd.DataFrame(
                data, columns=["x", "y", "spatial_cluster", target], index=self.sample_names,
            )

            temp_df[f"{target}_density"] = temp_df.groupby("spatial_cluster")[target].transform(
                lambda x: np.count_nonzero(x) / len(x)
            )

            # Stratified subsampling:
            sampled_df = pd.DataFrame()
            for stratum in temp_df["spatial_cluster"].unique():
                if len(set(temp_df[f"{target}_density"])) == 2:
                    stratum_df = temp_df[temp_df["spatial_cluster"] == stratum]
                    # Density of node feature in this stratum
                    node_feature_density = stratum_df[f"{target}_density"].iloc[0]

                    # Set total number of cells to subsample- sample at least 2x the number of zero cells as nonzeros:
                    # Sample size proportional to stratum size and node feature density:
                    n_sample_nonzeros = int(np.ceil((len(stratum_df) // 2) * (1 + (node_feature_density - 1))))
                    n_sample_zeros = 2 * n_sample_nonzeros
                    sample_size = n_sample_zeros + n_sample_nonzeros
                    sampled_stratum_df = stratum_df.sample(n=sample_size)
                    sampled_df = pd.concat([sampled_df, sampled_stratum_df])

                else:
                    stratum_df = temp_df[temp_df["spatial_cluster"] == stratum]
                    # Density of node feature in this stratum
                    node_feature_density = stratum_df[f"{target}_density"].iloc[0]

                    # Proportional sample size based on number of nonzeros- or three zero cells, depending on which
                    # is larger:
                    num_nonzeros = len(stratum_df[stratum_df[f"{target}_density"] > 0])
                    n_sample_nonzeros = int(np.ceil((num_nonzeros // 2) * (1 + (node_feature_density - 1))))
                    n_sample_zeros = np.maximum(2 * n_sample_nonzeros, 3)
                    sample_size = n_sample_zeros + n_sample_nonzeros

                    # Sample at least n_sample_zeros zeros if possible:
                    zero_sub = stratum_df[stratum_df[target] == 0]
                    n_zeros_sample = np.minimum(n_sample_zeros, len(zero_sub))
                    sampled_zero_stratum_df = zero_sub.sample(n=n_zeros_sample)

                    # Check if any nonzeros exist
                    stratum_nonzero_df = stratum_df[stratum_df[target] > 0]
                    if not stratum_nonzero_df.empty:
                        # Sample from nonzeros first
                        num_nonzeros_sampled = min(len(stratum_nonzero_df), sample_size - n_sample_zeros)
                        sampled_nonzero_stratum_df = stratum_nonzero_df.sample(n=num_nonzeros_sampled)

                        # Concatenate zeros and nonzeros:
                        sampled_stratum_df = pd.concat([sampled_nonzero_stratum_df, sampled_zero_stratum_df])
                    else:
                        sampled_stratum_df = sampled_zero_stratum_df

                    sampled_df = pd.concat([sampled_df, sampled_stratum_df])

            if self.comm.rank == 0:
                self.logger.info(f"For target {target} subsampled from {self.n_samples} to {len(sampled_df)} cells.")

            # Map each non-sampled point to its closest sampled point:
            distances = cdist(self.coords.astype(float), sampled_df[["x", "y"]].values.astype(float), "euclidean")
            closest_indices = np.argmin(distances, axis=1)

            # Dictionary where keys are indices of subsampled points and values are lists of indices of the original
            # points closest to them:
            closest_dict = {}
            for i, idx in enumerate(closest_indices):
                key = sampled_df.index[idx]
                if key not in closest_dict:
                    closest_dict[key] = []
                if self.sample_names[i] not in sampled_df.index:
                    closest_dict[key].append(self.sample_names[i])

            self.indices[target] = [self.sample_names.get_loc(name) for name in sampled_df.index]
            self.n_samples_fitted[target] = len(sampled_df)
            self.subsampled_sample_names[target] = sampled_df.index
            self.neighboring_unsampled[target] = closest_dict

        # Cast each of these dictionaries to all processes:
        self.indices = self.comm.bcast(self.indices, root=0)
        self.n_samples_fitted = self.comm.bcast(self.n_samples_fitted, root=0)
        self.subsampled_sample_names = self.comm.bcast(self.subsampled_sample_names, root=0)
        self.neighboring_unsampled = self.comm.bcast(self.neighboring_unsampled, root=0)

        # Set subsampled flag:
        self.subsampled = True

    def _set_search_range(self, signaling_type: Optional[str] = None):
        """Set the search range for the bandwidth selection procedure.

        Args:
            signaling_type: Optional category for the interaction, one of "Cell-Cell Contact", "Diffusive Signaling"
                (umbrella term for Secreted Signaling + ECM-Receptor), "Secreted Signaling" or "ECM-Receptor"
        """

        if self.adata_path is not None:
            if signaling_type is None:
                signaling_type = self.signaling_types

            # Check whether the signaling types defined are membrane-bound or are composed of soluble molecules:
            if signaling_type == "Cell-Cell Contact":
                # Signaling is limited to occurring between only the nearest neighbors of each cell:
                if self.bw_fixed:
                    distances = cdist(self.coords, self.coords)
                    # Set max bandwidth to the average distance to the 20 nearest neighbors:
                    nearest_idxs_all = np.argpartition(distances, 21, axis=1)[:, 1:21]
                    nearest_distances = np.take_along_axis(distances, nearest_idxs_all, axis=1)
                    self.maxbw = np.mean(nearest_distances, axis=1)

                    if self.minbw is None:
                        # Set min bandwidth to the average distance to the 5 nearest neighbors:
                        nearest_idxs_all = np.argpartition(distances, 6, axis=1)[:, 1:6]
                        nearest_distances = np.take_along_axis(distances, nearest_idxs_all, axis=1)
                        self.minbw = np.mean(nearest_distances, axis=1)
                else:
                    self.maxbw = 20

                    if self.minbw is None:
                        self.minbw = 5

                if self.minbw >= self.maxbw:
                    raise ValueError(
                        "The minimum bandwidth must be less than the maximum bandwidth. Please adjust the `minbw` "
                        "parameter accordingly."
                    )
                return

            # If the bandwidth is defined by a fixed spatial distance:
            if self.bw_fixed:
                max_dist = np.max(
                    np.array([np.max(cdist([self.coords[i]], self.coords)) for i in range(self.n_samples)])
                )
                # Set max bandwidth higher than the max distance between any two given samples:
                self.maxbw = max_dist * 2

                if self.minbw is None:
                    min_dist = np.min(
                        np.array(
                            [np.min(np.delete(cdist(self.coords[[i]], self.coords), i)) for i in range(self.n_samples)]
                        )
                    )
                    self.minbw = min_dist / 2

            # If the bandwidth is defined by a fixed number of neighbors (and thus adaptive in terms of radius):
            else:
                if self.maxbw is None:
                    self.maxbw = 100

                if self.minbw is None:
                    self.minbw = 5

            if self.minbw >= self.maxbw:
                raise ValueError(
                    "The minimum bandwidth must be less than the maximum bandwidth. Please adjust the `minbw` "
                    "parameter accordingly."
                )

        else:
            # For regression on non-AnnData objects, repeat the above conditional bandwidth definition:
            if self.bw_fixed:
                max_dist = np.max(
                    np.array([np.max(cdist([self.coords[i]], self.coords)) for i in range(self.n_samples)])
                )
                # Set max bandwidth higher than the max distance between any two given samples:
                self.maxbw = max_dist * 2

                if self.minbw is None:
                    min_dist = np.min(
                        np.array(
                            [np.min(np.delete(cdist(self.coords[[i]], self.coords), i)) for i in range(self.n_samples)]
                        )
                    )
                    self.minbw = min_dist / 2

            # If the bandwidth is defined by a fixed number of neighbors (and thus adaptive in terms of radius):
            else:
                if self.maxbw is None:
                    self.maxbw = 100

                if self.minbw is None:
                    self.minbw = 5

            if self.minbw >= self.maxbw:
                raise ValueError(
                    "The minimum bandwidth must be less than the maximum bandwidth. Please adjust the `minbw` "
                    "parameter accordingly."
                )

    def _compute_all_wi(self, bw: Union[float, int]) -> scipy.sparse.spmatrix:
        """Compute spatial weights for all samples in the dataset given a specified bandwidth.

        Args:
            bw: Bandwidth for the spatial kernel

        Returns:
            wi: Array of weights for all samples in the dataset
        """

        # Parallelized computation of spatial weights for all samples:
        if not self.bw_fixed:
            self.logger.info(
                "Note that 'fixed' was not selected for the bandwidth estimation. Input to 'bw' will be "
                "taken to be the number of nearest neighbors to use in the bandwidth estimation."
            )

        get_wi_partial = partial(
            get_wi,
            n_samples=self.n_samples,
            coords=self.coords,
            fixed_bw=self.bw_fixed,
            exclude_self=self.exclude_self,
            kernel=self.kernel,
            bw=bw,
            threshold=0.01,
            sparse_array=True,
        )

        with Pool() as pool:
            weights = pool.map(get_wi_partial, range(self.n_samples))
        w = scipy.sparse.vstack(weights)
        return w

    def _compute_niche_mat(self) -> np.ndarray:
        """Compute the niche matrix for the dataset."""
        # Compute "presence" of each cell type in the neighborhood of each sample:
        dmat_neighbors = self.all_spatial_weights.dot(self.cell_categories.values)
        return dmat_neighbors

    def _adjust_x(self):
        """Adjust the independent variable array based on the defined bandwidth."""

        # If applicable, use the cell type category array to encode the niche of each sample:
        if self.mod_type == "niche":
            self.X = self._compute_niche_mat()
            # If feature names doesn't already exist, create it:
            if not hasattr(self, "feature_names"):
                self.feature_names = self.cell_categories.columns

        # If applicable, use the ligand expression array, the receptor expression array and the spatial weights array
        # to compute the ligand-receptor expression signature of each spatial neighborhood:
        elif self.mod_type == "lr":
            X_df = pd.DataFrame(
                np.zeros((self.n_samples, len(self.lr_pairs))), columns=self.feature_names, index=self.adata.obs_names
            )

            for lr_pair in self.lr_pairs:
                lig, rec = lr_pair[0], lr_pair[1]
                lig_expr_values = scipy.sparse.csr_matrix(self.ligands_expr[lig].values.reshape(-1, 1))
                rec_expr_values = scipy.sparse.csr_matrix(self.receptors_expr[rec].values.reshape(-1, 1))

                # Communication signature b/w receptor in target and ligand in neighbors:
                lr_product = np.dot(rec_expr_values, lig_expr_values.T)
                # Neighborhood mask:
                X_df[f"{lig}-{rec}"] = scipy.sparse.csr_matrix.sum(
                    scipy.sparse.csr_matrix.multiply(self.all_spatial_weights, lr_product), axis=1
                ).A.flatten()

            self.X = X_df.values
            # If feature names doesn't already exist, create it:
            if not hasattr(self, "feature_names"):
                self.feature_names = [pair[0] + "-" + pair[1] for pair in self.lr_pairs]
            # If list of L:R labels (secreted vs. membrane-bound vs. ECM) doesn't already exist, create it:
            if not hasattr(self, "self.signaling_types"):
                self.signaling_types = self.lr_db.loc[
                    (self.lr_db["from"].isin([x[0] for x in self.lr_pairs]))
                    & (self.lr_db["to"].isin([x[1] for x in self.lr_pairs])),
                    "type",
                ].tolist()

        # If applicable, combine the ideas of the above two models:
        elif self.mod_type == "slice":
            # Each ligand-receptor pair will have an associated niche matrix:
            niche_mats = {}

            for lr_pair in self.lr_pairs:
                lig, rec = lr_pair[0], lr_pair[1]
                lig_expr_values = scipy.sparse.csr_matrix(self.ligands_expr[lig].values.reshape(-1, 1))
                rec_expr_values = scipy.sparse.csr_matrix(self.receptors_expr[rec].values.reshape(-1, 1))
                # Multiply one-hot category array by the expression of select ligand to get a cell type-specific
                # ligand expression matrix:
                lig_expr = np.multiply(
                    self.cell_categories, np.tile(lig_expr_values.toarray(), self.cell_categories.shape[1])
                )
                # Tile receptor expression (for elementwise multiplication with the ligand expression matrix):
                rec_expr = np.tile(rec_expr_values.toarray(), self.cell_categories.shape[1])

                # Multiply adjacency matrix by the cell-specific expression of select ligand:
                nbhd_lig_expr = self.all_spatial_weights.dot(lig_expr)
                # Multiply by receptor expression to get a description of the ligand-receptor presence in each
                # neighborhood:
                niche_lr = np.multiply(nbhd_lig_expr, rec_expr)
                # Indicate which cell type ligand expression comes from:
                lr_connections_cols = [f"{i}-{lig}:{rec}" for i in self.cell_categories.columns]
                # Add to dictionary of niche matrices:
                niche_mats[f"{lig}-{rec}"] = pd.DataFrame(niche_lr, columns=lr_connections_cols)

            # Combine the niche matrices for each ligand-receptor pair:
            self.X = pd.concat(niche_mats.values(), axis=1)
            self.X.index = self.adata.obs_names
            n_cols = self.X.shape[1]

            # Drop all-zero columns (represent cell type pairs with no spatial coupled L/R expression):
            self.X = self.X.loc[:, (self.X != 0).any(axis=0)]
            self.feature_names = self.X.columns.tolist()
            self.logger.info(
                f"Dropped all-zero columns from cell type-specific signaling array, from {n_cols} to "
                f"{self.X.shape[1]}."
            )

            # If :attr `multicollinear_threshold` is given, drop multicollinear features:
            if self.multicollinear_threshold is not None:
                self.X = multicollinearity_check(self.X, self.multicollinear_threshold, logger=self.logger)

            self.X = self.X.values

            # If list of L:R labels (secreted vs. membrane-bound vs. ECM) doesn't already exist, create it:
            if not hasattr(self, "self.signaling_types"):
                self.signaling_types = []
                for col in self.feature_names:
                    match = re.search(r"(\w+)-(\w+):(\w+)", col)
                    ligrec = f"{match.group(2)}-{match.group(3)}"
                    result = self.lr_db.loc[
                        (self.lr_db["from"] == ligrec.split("-")[0]) & (self.lr_db["to"] == ligrec.split("-")[1]),
                        "type",
                    ].iloc[0]

                    self.signaling_types.append(result)

        # Optionally, add continuous covariate value for each cell:
        if self.covariate_keys is not None:
            matched_obs = []
            matched_var_names = []
            for key in self.covariate_keys:
                if key in self.adata.obs:
                    matched_obs.append(key)
                elif key in self.adata.var_names:
                    matched_var_names.append(key)
                else:
                    self.logger.info(
                        f"Specified covariate key '{key}' not found in adata.obs. Not adding this "
                        f"covariate to the X matrix."
                    )
            matched_obs_matrix = self.adata.obs[matched_obs].to_numpy()
            matched_var_matrix = self.adata[:, matched_var_names].X.toarray()
            cov_names = matched_obs + matched_var_names
            concatenated_matrix = np.concatenate((matched_obs_matrix, matched_var_matrix), axis=1)
            self.X = np.concatenate((self.X, concatenated_matrix), axis=1)
            self.feature_names += cov_names

        # Add intercept if applicable:
        if self.fit_intercept:
            self.X = np.concatenate((np.ones((self.X.shape[0], 1)), self.X), axis=1)
            self.feature_names = ["intercept"] + self.feature_names

        self.n_features = self.X.shape[1]
        # Rebroadcast the number of features to fit:
        self.n_features = self.comm.bcast(self.n_features, root=0)
        # Broadcast secreted vs. membrane-bound reference:
        if hasattr(self, "self.signaling_types"):
            # If all features are assumed to operate on the same length scale, there should not be a mix of secreted
            # and membrane-bound-mediated signaling:
            if not self.multiscale_flag:
                # Secreted + ECM-receptor can diffuse across larger distances, but membrane-bound interactions are
                # limited by non-diffusivity. Therefore, it is not advisable to include a mixture of membrane-bound with
                # either of the other two categories in the same model.
                if (
                    "Cell-Cell Contact" in set(self.signaling_types)
                    and "Secreted Signaling" in set(self.signaling_types)
                ) or ("Cell-Cell Contact" in set(self.signaling_types) and "ECM-Receptor" in set(self.signaling_types)):
                    raise ValueError(
                        "It is not advisable to include a mixture of membrane-bound with either secreted or "
                        "ECM-receptor in the same model because the valid distance scales over which they operate "
                        "is different. If you wish to include both, please run the model twice, once for each category."
                    )

                self.signaling_types = set(self.signaling_types)
                if "Secred Signaling" in self.signaling_types or "ECM-Receptor" in self.signaling_types:
                    self.signaling_types = "Diffusive Signaling"
                else:
                    self.signaling_types = "Cell-Cell Contact"
            self.signaling_types = self.comm.bcast(self.signaling_types, root=0)

    def _adjust_x_nbhd_convolve(self, y: np.ndarray, X: np.ndarray):
        """Adjust the independent and dependent variable arrays based on the defined bandwidth. Used specifically to
        incorporate the neighborhood values of X and y for cell-intrinsic models. As models that use this inherit
        from this class, y and X need to be manually provided.

        Returns:
            y: Adjusted dependent variable array
            X: Adjusted independent variable array
        """
        y = self.all_spatial_weights.dot(y)
        for i in range(X.shape[1]):
            X[:, i] = self.all_spatial_weights.dot(X[:, i])

        return y, X

    def hessian(self, fitted: np.ndarray) -> np.ndarray:
        """Compute the Hessian matrix for the given model, representing the confidence in the parameter estimates.

        Args:
            fitted: Array of shape [n_samples,]; fitted mean response variable (link function evaluated
                at the linear predicted values)

        Returns:
            hessian: Hessian matrix
        """
        if self.distr == "gaussian":
            hessian = np.dot(self.X.T, self.X)
        elif self.distr == "poisson":
            hessian = np.dot(self.X.T, np.dot(np.diag(fitted), self.X))
        elif self.distr == "nb":
            hessian = np.dot(self.X.T, np.dot(np.diag(fitted * (1 + fitted / self.distr_obj.variance.disp)), self.X))
        return hessian

    def local_fit(
        self, i: int, y: np.ndarray, X: np.ndarray, bw: Union[float, int], final: bool = False, multiscale: bool = False
    ) -> Union[np.ndarray, List[float]]:
        """Fit a local regression model for each sample.

        Args:
            i: Index of sample for which local regression model is to be fitted
            y: Response variable
            X: Independent variable array
            bw: Bandwidth for the spatial kernel
            final: Set True to indicate that no additional parameter selection needs to be performed; the model can
                be fit and more stats can be returned.
            multiscale: Set True to fit a multiscale GWR model where the independent-dependent relationships can vary
            over
                different spatial scales

        Returns:
            A single output will be given for each case, and can contain either `betas` or a list w/ combinations of
            the following:
                - i: Index of sample for which local regression model was fitted
                - diagnostic: Portion of the output to be used for diagnostic purposes- for Gaussian regression,
                    this is the residual for the fitted response variable value compared to the observed value. For
                    non-Gaussian generalized linear regression, this is the fitted response variable value (which
                    will be used to compute deviance and log-likelihood later on).
                - hat_i: Row i of the hat matrix, which is the effect of deleting sample i from the dataset on the
                    estimated predicted value for sample i
                - bw_diagnostic: Output to be used for diagnostic purposes during bandwidth selection- for Gaussian
                    regression, this is the squared residual, for non-Gaussian generalized linear regression,
                    this is the fitted response variable value. One of the returns if :param `final` is False
                - betas: Estimated coefficients for sample i- if :param `multiscale` is True, betas is the only return
                - leverages: Leverages for sample i, representing the influence of each independent variable on the
                    predicted values (linear predictor for GLMs, response variable for Gaussian regression).
        """
        # Reshape y if necessary:
        if self.n_features > 1:
            y = y.reshape(-1, 1)

        # Name of this sample:
        sample_name = self.sample_names[i]

        wi = get_wi(
            i, n_samples=self.n_samples, coords=self.coords, fixed_bw=self.bw_fixed, kernel=self.kernel, bw=bw
        ).reshape(-1, 1)

        if self.distr == "gaussian":
            betas, pseudoinverse = compute_betas_local(y, X, wi)
            pred_y = np.dot(X[i], betas)
            residual = y[i] - pred_y
            diagnostic = residual

            # Reshape coefficients if necessary:
            betas = betas.flatten()
            # Effect of deleting sample i from the dataset on the estimated predicted value at sample i:
            hat_i = np.dot(X[i], pseudoinverse[:, i])

        elif self.distr == "poisson" or self.distr == "nb":
            # init_betas (initial coefficients) to be incorporated at runtime:
            betas, y_hat, _, final_irls_weights, _, _, pseudoinverse = iwls(
                y,
                X,
                distr=self.distr,
                init_betas=self.init_betas,
                tol=self.tolerance,
                max_iter=self.max_iter,
                spatial_weights=wi,
                link=None,
                alpha=self.alpha,
                tau=None,
            )
            # if (i + 1) % 1000 == 0 or i == self.n_samples - 1:
            #     self.logger.info(f"Completed IWLS fitting for sample {i+1} / {self.n_samples}.")

            # Reshape coefficients if necessary:
            betas = betas.flatten()
            pred_y = y_hat[i]
            diagnostic = pred_y
            # Effect of deleting sample i from the dataset on the estimated predicted value at sample i:
            hat_i = np.dot(X[i], pseudoinverse[:, i]) * final_irls_weights[i][0]

        else:
            raise ValueError("Invalid `distr` specified. Must be one of 'gaussian', 'poisson', or 'nb'.")

        # Squared singular values:
        lvg = np.diag(np.dot(pseudoinverse, pseudoinverse.T)).reshape(-1)

        if final:
            if multiscale:
                return betas
            return np.concatenate(([sample_name, diagnostic, hat_i], betas, lvg))
        else:
            # For bandwidth optimization:
            if self.distr == "gaussian":
                bw_diagnostic = residual * residual
            elif self.distr == "poisson" or self.distr == "nb":
                # Else just return fitted value for diagnostic purposes:
                bw_diagnostic = pred_y
            return [bw_diagnostic, hat_i]

    def find_optimal_bw(self, range_lowest: float, range_highest: float, function: Callable) -> float:
        """Perform golden section search to find the optimal bandwidth.

        Args:
            range_lowest: Lower bound of the search range
            range_highest: Upper bound of the search range
            function: Function to be minimized

        Returns:
            bw: Optimal bandwidth
        """
        delta = 0.38197
        new_lb = range_lowest + delta * np.abs(range_highest - range_lowest)
        new_ub = range_highest - delta * np.abs(range_highest - range_lowest)

        score = None
        optimum_bw = None
        difference = 1.0e9
        iterations = 0
        results_dict = {}

        while np.abs(difference) > self.tolerance and iterations < self.max_iter:
            iterations += 1

            # Bandwidth needs to be discrete:
            if not self.bw_fixed:
                new_lb = np.round(new_lb)
                new_ub = np.round(new_ub)

            if new_lb in results_dict:
                lb_score = results_dict[new_lb]
            else:
                # Return score metric (e.g. AICc) for the lower bound bandwidth:
                lb_score = function(new_lb)
                results_dict[new_lb] = lb_score

            if new_ub in results_dict:
                ub_score = results_dict[new_ub]
            else:
                # Return score metric (e.g. AICc) for the upper bound bandwidth:
                ub_score = function(new_ub)
                results_dict[new_ub] = ub_score

            if self.comm.rank == 0:
                # Follow direction of increasing score until score stops increasing:
                if lb_score <= ub_score:
                    # Set new optimum score and bandwidth:
                    optimum_score = lb_score
                    optimum_bw = new_lb

                    # Update new max upper bound and test lower bound:
                    range_highest = new_ub
                    new_ub = new_lb
                    new_lb = range_lowest + delta * np.abs(range_highest - range_lowest)

                # Else follow direction of decreasing score until score stops decreasing:
                else:
                    # Set new optimum score and bandwidth:
                    optimum_score = ub_score
                    optimum_bw = new_ub

                    # Update new max lower bound and test upper bound:
                    range_lowest = new_lb
                    new_lb = new_ub
                    new_ub = range_highest - delta * np.abs(range_highest - range_lowest)

                difference = lb_score - ub_score
                # Update new value for score:
                score = optimum_score
            # self.logger.info(f"Iteration {iterations}- optimum bandwidth: {optimum_bw}, difference: "
            #                  f"{np.abs(difference)}.")

            new_lb = self.comm.bcast(new_lb, root=0)
            new_ub = self.comm.bcast(new_ub, root=0)
            score = self.comm.bcast(score, root=0)
            difference = self.comm.bcast(difference, root=0)
            optimum_bw = self.comm.bcast(optimum_bw, root=0)

        return optimum_bw

    def mpi_fit(
        self,
        y: Optional[np.ndarray],
        X: Optional[np.ndarray],
        bw: Union[float, int],
        final: bool = False,
        multiscale: bool = False,
        y_label: Optional[str] = None,
    ) -> Union[None, np.ndarray]:
        """Fit local regression model for each sample in parallel, given a specified bandwidth.

        Args:
            y: Response variable
            X: Independent variable array- if not given, will default to :attr `X`. Note that if object was initialized
                using an AnnData object, this will be overridden with :attr `X` even if a different array is given.
            bw: Bandwidth for the spatial kernel
            final: Set True to indicate that no additional parameter selection needs to be performed; the model can
                be fit and more stats can be returned.
            multiscale: Set True to fit a multiscale GWR model where the independent-dependent relationships can vary
            over
                different spatial scales
            y_label: Optional, can be used to provide a unique ID for the dependent variable for saving purposes
        """
        # If model to be run is a "niche", "lr" or "slice" model, update the spatial weights and then update X given
        # the current value of the bandwidth:
        if X is None:
            if hasattr(self, "adata"):
                self.all_spatial_weights = self._compute_all_wi(bw)
                self.all_spatial_weights = self.comm.bcast(self.all_spatial_weights, root=0)
                self.logger.info(f"Adjusting X for new bandwidth: {bw}")
                self._adjust_x()
                self.X = self.comm.bcast(self.X, root=0)
                X = self.X
            else:
                X = self.X
        if X.shape[1] != self.n_features:
            n_features = X.shape[1]
            n_features = self.comm.bcast(n_features, root=0)
        else:
            n_features = self.n_features

        if self.grn:
            self.all_spatial_weights = self._compute_all_wi(bw)
            # Row standardize spatial weights so as to ensure results aren't biased by the number of neighbors of
            # each cell:
            self.all_spatial_weights = self.all_spatial_weights / self.all_spatial_weights.sum(axis=1)[:, None]
            y, X = self._adjust_x_nbhd_convolve(y, X)
            y = self.comm.bcast(y, root=0)
            X = self.comm.bcast(X, root=0)

        if final:
            if multiscale:
                local_fit_outputs = np.empty((self.x_chunk.shape[0], n_features), dtype=np.float64)
            else:
                local_fit_outputs = np.empty((self.x_chunk.shape[0], 2 * n_features + 3), dtype=np.float64)

            # Fitting for each location, or each location that is among the subsampled points:
            pos = 0
            for i in self.x_chunk:
                local_fit_outputs[pos] = self.local_fit(i, y, X, bw, final=final, multiscale=multiscale)
                pos += 1

            # Gather data to the central process such that an array is formed where each sample has its own
            # measurements:
            all_fit_outputs = self.comm.gather(local_fit_outputs, root=0)
            # For non-MGWR:
            # Column 0: Index of the sample
            # Column 1: Diagnostic (residual for Gaussian, fitted response value for Poisson/NB)
            # Column 2: Contribution of each sample to its own value
            # Columns 3-n_feats+3: Estimated coefficients
            # Columns n_feats+3-end: Canonical correlations
            # All columns are betas for MGWR

            # If multiscale, do not need to fit using fixed bandwidth:
            if multiscale:
                # For MGWR, this function is only needed to initialize parameters:
                all_fit_outputs = self.comm.bcast(all_fit_outputs, root=0)
                all_fit_outputs = np.vstack(all_fit_outputs)
                return all_fit_outputs

            if self.comm.rank == 0:
                all_fit_outputs = np.vstack(all_fit_outputs)
                self.logger.info(f"Computing metrics for GWR using bandwidth: {bw}")

                # Residual sum of squares for Gaussian model:
                if self.distr == "gaussian":
                    RSS = np.sum(all_fit_outputs[:, 1] ** 2)
                    # Total sum of squares:
                    TSS = np.sum((y[self.target_indices] - np.mean(y[self.target_indices])) ** 2)
                    r_squared = 1 - RSS / TSS

                    # Note: trace of the hat matrix and effective number of parameters (ENP) will be used
                    # interchangeably:
                    ENP = np.sum(all_fit_outputs[:, 2])
                    # Residual variance:
                    sigma_squared = RSS / (self.n_samples_fitted - ENP)
                    # Corrected Akaike Information Criterion:
                    aicc = self.compute_aicc_linear(RSS, ENP)
                    # Scale the leverages by their variance to compute standard errors of the predictor:
                    all_fit_outputs[:, -n_features:] = np.sqrt(all_fit_outputs[:, -n_features:] * sigma_squared)

                    # For saving outputs:
                    header = "name,residual,influence,"
                else:
                    r_squared = None

                if self.distr == "poisson" or self.distr == "nb":
                    # Deviance:
                    deviance = self.distr_obj.deviance(y[self.target_indices], all_fit_outputs[:, 1])
                    # Residual deviance:
                    residual_deviance = self.distr_obj.deviance_residuals(y[self.target_indices], all_fit_outputs[:, 1])
                    # Reshape if necessary:
                    if self.n_features > 1:
                        residual_deviance = residual_deviance.reshape(-1, 1)
                    # ENP:
                    ENP = np.sum(all_fit_outputs[:, 2])
                    # Corrected Akaike Information Criterion:
                    aicc = self.compute_aicc_glm(residual_deviance, ENP)
                    # To obtain standard errors for each coefficient, take the square root of the diagonal elements
                    # of the covariance matrix:
                    # Compute the covariance matrix using the Hessian- first compute the estimate for dispersion of
                    # the NB distribution:
                    if self.distr == "nb":
                        theta = 1 / self.distr_obj.variance(all_fit_outputs[:, 1])
                        weights = self.distr_obj.weights(all_fit_outputs[:, 1])
                        deviance = 2 * np.sum(
                            weights
                            * (
                                y[self.target_indices] * np.log(y[self.target_indices] / all_fit_outputs[:, 1])
                                + (theta - 1) * np.log(1 + all_fit_outputs[:, 1] / (theta - 1))
                            )
                        )
                        dof = len(y[self.target_indices]) - self.X.shape[1]
                        self.distr_obj.variance.disp = deviance / dof

                    hessian = self.hessian(all_fit_outputs[:, 1])
                    cov_matrix = np.linalg.inv(hessian)
                    all_fit_outputs[:, -n_features:] = np.sqrt(np.diag(cov_matrix))

                    # For saving outputs:
                    header = "name,prediction,influence,"
                else:
                    deviance = None

                # Save results:
                varNames = self.feature_names
                # Columns for the possible intercept, coefficients and squared canonical coefficients:
                for x in varNames:
                    header += "b_" + x + ","
                for x in varNames:
                    header += "se_" + x + ","

                # Return output diagnostics and save result:
                self.output_diagnostics(aicc, ENP, r_squared, deviance)
                self.save_results(all_fit_outputs, header, label=y_label)

            return

        # If not the final run:
        if self.distr == "gaussian":
            # Compute AICc using the sum of squared residuals:
            RSS = 0
            trace_hat = 0

            for i in self.x_chunk:
                fit_outputs = self.local_fit(i, y, X, bw, final=False)
                err_sq, hat_i = fit_outputs[0], fit_outputs[1]
                RSS += err_sq
                trace_hat += hat_i

            # Send data to the central process:
            RSS_list = self.comm.gather(RSS, root=0)
            trace_hat_list = self.comm.gather(trace_hat, root=0)

            if self.comm.rank == 0:
                RSS = np.sum(RSS_list)
                trace_hat = np.sum(trace_hat_list)
                aicc = self.compute_aicc_linear(RSS, trace_hat)
                if not multiscale:
                    self.logger.info(f"Bandwidth: {bw}, Linear AICc: {aicc}")
                return aicc

        elif self.distr == "poisson" or self.distr == "nb":
            # Compute AICc using the fitted and observed values:
            trace_hat = 0
            pos = 0
            y_pred = np.empty(self.x_chunk.shape[0], dtype=np.float64)

            for i in self.x_chunk:
                fit_outputs = self.local_fit(i, y, X, bw, final=False)
                y_pred_i, hat_i = fit_outputs[0], fit_outputs[1]
                y_pred[pos] = y_pred_i
                trace_hat += hat_i
                pos += 1

            # Send data to the central process:
            all_y_pred = self.comm.gather(y_pred, root=0)
            trace_hat_list = self.comm.gather(trace_hat, root=0)

            if self.comm.rank == 0:
                deviance_residuals = self.distr_obj.deviance_residuals(y[self.target_indices], all_y_pred)
                trace_hat = np.sum(trace_hat_list)
                aicc = self.compute_aicc_glm(deviance_residuals, trace_hat)
                if not multiscale:
                    self.logger.info(f"Bandwidth: {bw}, GLM AICc: {aicc}")
                return aicc

        return

    def fit(
        self,
        y: Optional[pd.DataFrame] = None,
        X: Optional[np.ndarray] = None,
        init_betas: Optional[Dict[str, np.ndarray]] = None,
        multiscale: bool = False,
        signaling_type: Optional[str] = None,
        verbose: bool = True,
    ) -> Optional[Tuple[Union[None, Dict[str, np.ndarray]], Dict[str, float]]]:
        """For each column of the dependent variable array, fit model. If given bandwidth, run :func
        `SWR.mpi_fit()` with the given bandwidth. Otherwise, compute optimal bandwidth using :func
        `SWR.select_optimal_bw()`, minimizing AICc.

        Args:
            y: Optional dataframe, can be used to provide dependent variable array directly to the fit function. If
                None, will use :attr `targets_expr` computed using the given AnnData object to create this (each
                individual column will serve as an independent variable). Needed to be given as a dataframe so that
                column(s) are labeled, so each result can be associated with a labeled dependent variable.
            X: Optional array, can be used to provide dependent variable array directly to the fit function. If
                None, will use :attr `X` computed using the given AnnData object and the type of the model to create.
            init_betas: Optional dictionary containing arrays with initial values for the coefficients. Keys should
                correspond to target genes and values should be arrays of shape [n_features, 1].
            multiscale: Set True to indicate that a multiscale model should be fitted
            signaling_type: Optional category for the interaction, one of "Cell-Cell Contact", "Diffusive Signaling"
                (umbrella term for Secreted Signaling + ECM-Receptor), "Secreted Signaling" or "ECM-Receptor".
            verbose: Set True to print out information about the bandwidth selection and/or fitting process. Will be
                False for most multiscale runs, but defaults to True.

        Returns:
            all_data: Dictionary containing outputs of :func `SWR.mpi_fit()` with the chosen or determined bandwidth-
                note that this will either be None or in the case that :param `multiscale` is True, an array of shape [
                n_samples, n_features] representing the coefficients for each sample (if :param `multiscale` is False,
                these arrays will instead be saved to file).
            all_bws: Dictionary containing outputs in the case that bandwidth is not already known, resulting from
                the conclusion of the optimization process.
        """

        if not self.set_up:
            self.logger.info("Model has not yet been set up to run, running :func `SWR._set_up_model()` now...")
            self._set_up_model()

        if y is None:
            y_arr = self.targets_expr if hasattr(self, "targets_expr") else self.target
        else:
            y_arr = y
            y_arr = self.comm.bcast(y_arr, root=0)

        if X is None:
            X = self.X
        else:
            X = self.comm.bcast(X, root=0)

        # Compute fit for each column of the dependent variable array individually- store each output array (if
        # applicable, i.e. if :param `multiscale` is True) and optimal bandwidth (also if applicable, i.e. if :param
        # `multiscale` is True):
        all_data, all_bws = {}, {}

        for target in y_arr.columns:
            y = y_arr[target].values
            y = self.comm.bcast(y, root=0)

            # If subsampled, define the appropriate chunk of the right subsampled array for this process:
            if self.subsampled:
                self.target_indices = np.array(self.indices[target])
                n_samples_fitted_target = self.n_samples_fitted[target]
                chunk_size = int(math.ceil(float(n_samples_fitted_target) / self.comm.size))
                # Assign chunks to each process:
                self.x_chunk = self.target_indices[self.comm.rank * chunk_size : (self.comm.rank + 1) * chunk_size]

            # Check for initial weights:
            if init_betas is not None:
                self.init_betas = init_betas[target].reshape(-1, 1)

            if self.bw is not None:
                if verbose:
                    self.logger.info(f"Starting fitting process for target {target}. Initial bandwidth: {self.bw}.")
                # If bandwidth is already known, run the main fit function:
                self.mpi_fit(y, X, self.bw, final=True)
                return

            if self.comm.rank == 0:
                if verbose:
                    self.logger.info(
                        f"Starting fitting process for target {target}. First finding optimal " f"bandwidth..."
                    )
                self._set_search_range(signaling_type=signaling_type)
                if not multiscale:
                    self.logger.info(f"Calculated bandwidth range over which to search: {self.minbw}-{self.maxbw}.")
            self.minbw = self.comm.bcast(self.minbw, root=0)
            self.maxbw = self.comm.bcast(self.maxbw, root=0)

            # Searching for optimal bandwidth- set final=False to return AICc for each run of the optimization
            # function:
            fit_function = lambda bw: self.mpi_fit(y, X, bw, final=False, multiscale=multiscale)
            optimal_bw = self.find_optimal_bw(self.minbw, self.maxbw, fit_function)
            if self.bw_fixed:
                optimal_bw = round(optimal_bw, 2)

            data = self.mpi_fit(y, X, optimal_bw, final=True, multiscale=multiscale, y_label=target)
            if data is not None:
                all_data[target] = data
            all_bws[target] = optimal_bw

        return all_data, all_bws

    def predict(
        self, input: Optional[np.ndarray] = None, coeffs: Optional[Union[np.ndarray, Dict[str, pd.DataFrame]]] = None
    ) -> pd.DataFrame:
        """Given input data and learned coefficients, predict the dependent variables.

        Args:
            input: Input data to be predicted on.
            coeffs: Coefficients to be used in the prediction. If None, will attempt to load the coefficients learned
                in the fitting process from file.
        """
        if input is None:
            input = self.X

        if coeffs is None:
            coeffs = self.return_outputs()
            # If dictionary, compute outputs for the multiple dependent variables and concatenate together:
            if isinstance(coeffs, Dict):
                all_y_pred = pd.DataFrame(index=self.sample_names, columns=coeffs.keys())
                for target in coeffs:
                    if input.shape[0] != coeffs[target].shape[0]:
                        raise ValueError(
                            f"Input data has {input.shape[0]} samples but coefficients for target {target} have "
                            f"{coeffs[target].shape[0]} samples."
                        )
                    y_pred = np.sum(input * coeffs[target], axis=1)
                    if self.distr != "gaussian":
                        y_pred = self.distr_obj.predict(y_pred)
                    all_y_pred = pd.concat([all_y_pred, y_pred], axis=1)
                return all_y_pred

            else:
                if self.distr == "gaussian":
                    y_pred_all = input * coeffs
                else:
                    y_pred_all_nontransformed = input * coeffs
                    y_pred_all = self.distr_obj.predict(y_pred_all_nontransformed)
                y_pred = pd.DataFrame(np.sum(y_pred_all, axis=1), index=self.sample_names, columns=["y_pred"])
                return y_pred

    # ---------------------------------------------------------------------------------------------------
    # Diagnostics
    # ---------------------------------------------------------------------------------------------------
    def compute_aicc_linear(self, RSS: float, trace_hat: float, n_samples: Optional[int] = None) -> float:
        """Compute the corrected Akaike Information Criterion (AICc) for the linear GWR model."""
        if n_samples is None:
            n_samples = self.n_samples

        aicc = (
            n_samples * np.log(RSS / n_samples)
            + n_samples * np.log(2 * np.pi)
            + n_samples * (n_samples + trace_hat) / (n_samples - trace_hat - 2.0)
        )

        return aicc

    def compute_aicc_glm(self, resid_dev: np.ndarray, trace_hat: float, n_samples: Optional[int] = None) -> float:
        """Compute the corrected Akaike Information Criterion (AICc) for the generalized linear GWR models."""
        if n_samples is None:
            n_samples = self.n_samples

        aicc = (
            np.sum(resid_dev**2)
            + 2.0 * trace_hat
            + 2.0 * trace_hat * (trace_hat + 1.0) / (n_samples - trace_hat - 1.0)
        )

        return aicc

    def output_diagnostics(
        self,
        aicc: Optional[float] = None,
        ENP: Optional[float] = None,
        r_squared: Optional[float] = None,
        deviance: Optional[float] = None,
        y_label: Optional[str] = None,
    ) -> None:
        """Output diagnostic information about the GWR model."""

        if y_label is None:
            y_label = self.distr

        if aicc is not None:
            self.logger.info(f"Corrected Akaike information criterion for {y_label} model: {aicc}")

        if ENP is not None:
            self.logger.info(f"Effective number of parameters for {y_label} model: {ENP}")

        # Print R-squared for Gaussian assumption:
        if self.distr == "gaussian":
            if r_squared is None:
                raise ValueError(":param `r_squared` must be provided when performing Gaussian regression.")
            self.logger.info(f"R-squared for {y_label} model: {r_squared}")
        # Else log the deviance:
        else:
            if deviance is None:
                raise ValueError(":param `deviance` must be provided when performing non-Gaussian regression.")
            self.logger.info(f"Deviance for {y_label} model: {deviance}")

    # ---------------------------------------------------------------------------------------------------
    # Save to file
    # ---------------------------------------------------------------------------------------------------
    def save_results(self, data: np.ndarray, header: str, label: Optional[str]) -> None:
        """Save the results of the GWR model to file, and return the coefficients.

        Args:
            data: Elements of data to save to .csv
            header: Column names
            label: Optional, can be used to provide unique ID to save file- notably used when multiple dependent
                variables with different names are fit during this process.

        Returns:
            betas: Model coefficients
        """
        # Check if output_path was left as the default:
        if os.path.dirname(self.output_path) == "./output":
            if not os.path.exists("./output"):
                os.makedirs("./output")

        # If output path already has files in it, clear them:
        output_dir = os.path.dirname(self.output_path)
        if os.listdir(output_dir):
            # If there are files, delete them
            for file_name in os.listdir(output_dir):
                file_path = os.path.join(output_dir, file_name)
                if os.path.isfile(file_path):
                    os.remove(file_path)

        if label is not None:
            path = os.path.splitext(self.output_path)[0] + f"_{label}" + os.path.splitext(self.output_path)[1]
        else:
            path = self.output_path

        if self.comm.rank == 0:
            # Save to .csv:
            np.savetxt(path, data, delimiter=",", header=header[:-1], comments="")

    def predict_and_save(
        self, input: Optional[np.ndarray] = None, coeffs: Optional[Union[np.ndarray, Dict[str, pd.DataFrame]]] = None
    ):
        """Given input data and learned coefficients, predict the dependent variables and then save the output.

        Args:
            input: Input data to be predicted on.
            coeffs: Coefficients to be used in the prediction. If None, will attempt to load the coefficients learned
                in the fitting process from file.
        """
        y_pred = self.predict(input, coeffs)
        # Save to parent directory of the output path:
        parent_dir = os.path.dirname(self.output_path)
        pred_path = os.path.join(parent_dir, "predictions.csv")
        y_pred.to_csv(pred_path)

    def return_outputs(self) -> Union[pd.DataFrame, Dict[str, pd.DataFrame]]:
        """Return final coefficients for all fitted models."""
        parent_dir = os.path.dirname(self.output_path)
        all_coeffs = {}

        file_list = [f for f in os.listdir(parent_dir) if os.path.isfile(os.path.join(parent_dir, f))]
        for file in file_list:
            target = file.split("_")[-1][:-4]
            all_outputs = pd.read_csv(os.path.join(parent_dir, file), index_col=0)
            betas = all_outputs[[col for col in all_outputs.columns if col.startswith("b_")]]

            # If subsampling was performed, extend coefficients to non-sampled neighboring points:
            if self.subsampled:
                sampled_to_nonsampled_map = self.neighboring_unsampled[target]
                betas = betas.reindex(self.X, columns=betas.columns, fill_value=0)
                for sampled_idx, nonsampled_idxs in sampled_to_nonsampled_map.items():
                    for nonsampled_idx in nonsampled_idxs:
                        betas.loc[nonsampled_idx] = betas.loc[sampled_idx]

            # Save coefficients to dictionary:
            all_coeffs[target] = betas

        return all_coeffs

    def return_intercepts(self) -> Union[None, np.ndarray, Dict[str, np.ndarray]]:
        """Return final intercepts for all fitted models."""
        if not self.fit_intercept:
            self.logger.info("No intercepts were fit, returning None.")
            return

        parent_dir = os.path.dirname(self.output_path)
        all_intercepts = {}
        for file in os.listdir(parent_dir):
            all_outputs = pd.read_csv(os.path.join(parent_dir, file), index_col=0)
            intercepts = all_outputs["intercept"].values

            # If there were multiple dependent variables, save coefficients to dictionary:
            if file != os.path.basename(self.output_path):
                all_intercepts[file.split("_")[-1]] = intercepts
            else:
                all_intercepts = intercepts

        return all_intercepts


# Multiscale Spatially-weighted Inference of Cell-cell communication:
class MuSIC(SWR):
    """Modified version of the spatially weighted regression on spatial omics data with parallel processing,
    enabling each feature to have its own distinct spatial scale parameter. Runs after being called from the command
    line.

    Args:
        comm: MPI communicator object initialized with mpi4py, to control parallel processing operations
        parser: ArgumentParser object initialized with argparse, to parse command line arguments for arguments
            pertinent to modeling.

    Attributes:
        mod_type: The type of model that will be employed- this dictates how the data will be processed and
            prepared. Options:
                - "niche": Spatially-aware, uses spatial connections between samples as independent variables
                - "lr": Spatially-aware, uses the combination of receptor expression in the "target" cell and spatially
                    lagged ligand expression in the neighboring cells as independent variables.
                - "slice": Spatially-aware, uses a coupling of spatial category connections, ligand expression
                    and receptor expression to perform regression on select receptor-downstream genes.


        adata_path: Path to the AnnData object from which to extract data for modeling
        csv_path: Can also be used to specify path to non-AnnData .csv object. Assumes the first three columns
            contain x- and y-coordinates and then dependent variable values, in that order, with all subsequent
            columns containing independent variable values.
        normalize: Set True to Perform library size normalization, to set total counts in each cell to the same
            number (adjust for cell size). It is advisable not to do this if performing Poisson or negative binomial
            regression.
        smooth: Set True to correct for dropout effects by leveraging gene expression neighborhoods to smooth
            expression. It is advisable not to do this if performing Poisson or negative binomial regression.
        log_transform: Set True if log-transformation should be applied to expression. It is advisable not to do
            this if performing Poisson or negative binomial regression.
        target_expr_threshold: Only used if :param `mod_type` is "lr" or "slice" and :param `targets_path` is not
            given. When manually selecting targets, expression above a threshold percentage of cells will be used to
            filter to a smaller subset of interesting genes. Defaults to 0.2.


        custom_lig_path: Optional path to a .txt file containing a list of ligands for the model, separated by
            newlines. Only used if :attr `mod_type` is "lr" or "slice" (and thus uses ligand/receptor expression
            directly in the inference). If not provided, will select ligands using a threshold based on expression
            levels in the data.
        custom_rec_path: Optional path to a .txt file containing a list of receptors for the model, separated by
            newlines. Only used if :attr `mod_type` is "lr" or "slice" (and thus uses ligand/receptor expression
            directly in the inference). If not provided, will select receptors using a threshold based on expression
            levels in the data.
        custom_pathways_path: Rather than  providing a list of receptors, can provide a list of signaling pathways-
            all receptors with annotations in this pathway will be included in the model. Only used if :attr `mod_type`
            is "lr" or "slice".
        targets_path: Optional path to a .txt file containing a list of prediction target genes for the model,
            separated by newlines. If not provided, targets will be strategically selected from the given receptors.
        init_betas_path: Optional path to a .npy file containing initial coefficient values for the model. Initial
            coefficients should have shape [n_features, ].


        cci_dir: Full path to the directory containing cell-cell communication databases
        species: Selects the cell-cell communication database the relevant ligands will be drawn from. Options:
                "human", "mouse".
        output_path: Full path name for the .csv file in which results will be saved


        coords_key: Key in .obsm of the AnnData object that contains the coordinates of the cells
        group_key: Key in .obs of the AnnData object that contains the category grouping for each cell
        covariate_keys: Can be used to optionally provide any number of keys in .obs or .var containing a continuous
            covariate (e.g. expression of a particular TF, avg. distance from a perturbed cell, etc.)


        minbw: For use in automated bandwidth selection- the lower-bound bandwidth to test.
        maxbw: For use in automated bandwidth selection- the upper-bound bandwidth to test.


        distr: Distribution family for the dependent variable; one of "gaussian", "poisson", "nb"
        kernel: Type of kernel function used to weight observations; one of "bisquare", "exponential", "gaussian",
            "quadratic", "triangular" or "uniform".


        bw_fixed: Set True for distance-based kernel function and False for nearest neighbor-based kernel function
        exclude_self: If True, ignore each sample itself when computing the kernel density estimation
        fit_intercept: Set True to include intercept in the model and False to exclude intercept
    """

    def __init__(self, comm: MPI.Comm, parser: argparse.ArgumentParser):
        super().__init__(comm, parser)

    def multiscale_backfitting(
        self,
        y: Optional[pd.DataFrame] = None,
        X: Optional[np.ndarray] = None,
        init_betas: Optional[Dict[str, np.ndarray]] = None,
    ):
        """
        Backfitting algorithm for MGWR, obtains parameter estimates and variate-specific bandwidths by iterating one
        predictor while holding all others constant. Run before :func `fit` to obtain initial covariate-specific
        bandwidths.

        Reference: Fotheringham et al. 2017. Annals of AAG.

        Args:
            y: Optional dataframe, can be used to provide dependent variable array directly to the fit function. If
                None, will use :attr `targets_expr` computed using the given AnnData object to create this (each
                individual column will serve as an independent variable). Needed to be given as a dataframe so that
                column(s) are labeled, so each result can be associated with a labeled dependent variable.
            X: Optional array, can be used to provide dependent variable array directly to the fit function. If
                None, will use :attr `X` computed using the given AnnData object and the type of the model to create.
            init_betas: Optional dictionary containing arrays with initial values for the coefficients. Keys should
                correspond to target genes and values should be arrays of shape [n_features, 1].
        """
        if self.comm.rank == 0:
            self.logger.info("Multiscale Backfitting...")
            self.logger.info("Finding uniform initial bandwidth for all features...")

        # Initialize parameters, with a uniform initial bandwidth for all features:
        all_betas, all_bws = self.fit(multiscale=True, init_betas=init_betas)

        self.all_bws_init = all_bws

        if self.comm.rank == 0:
            self.logger.info("Initialization complete.")

        self.all_bws_history = {}
        self.params_all_targets = {}
        self.errors_all_targets = {}
        self.predictions_all_targets = {}
        # For linear models:
        self.all_RSS = {}
        self.all_TSS = {}

        # For GLM models:
        self.all_deviances = {}
        self.all_residual_deviances = {}

        # Optional, to save the dispersion parameter for negative binomial fitted to each target:
        self.nb_disp_dict = {}

        if y is None:
            y_arr = self.targets_expr if hasattr(self, "targets_expr") else self.target
        else:
            y_arr = y
            y_arr = self.comm.bcast(y_arr, root=0)

        if X is None:
            X = self.X
        else:
            X = self.comm.bcast(X, root=0)

        for target in y_arr.columns:
            y = y_arr[target].values
            y_label = target + "_multiscale_backfitting"

            # If subsampled, only fit on the subsampled indices:
            if self.subsampled:
                indices = self.indices[target]
            else:
                indices = np.arange(self.n_samples)

            # Initial values- multiply input by the array corresponding to the correct target:
            linear_predictors = X[indices, :] * all_betas[target]
            # Array of shape [n_samples, n_features] containing initial spatially-weighted regression predictions:
            if self.distr != "gaussian":
                y_pred_init = self.distr_obj.predict(linear_predictors)
            else:
                y_pred_init = linear_predictors
            all_pred_y = y_pred_init.sum(axis=1)

            error = y[indices].reshape(-1) - all_pred_y
            bws = [None] * self.n_features
            bw_plateau_counter = 0
            bw_history = []

            n_iters = max(200, self.max_iter)
            for iter in range(1, n_iters + 1):
                new_ys = np.empty(linear_predictors.shape, dtype=np.float64)
                new_betas = np.empty(linear_predictors.shape, dtype=np.float64)

                for n_feat in range(self.n_features):
                    self.logger.info(f"Backfitting for independent feature {self.feature_names[n_feat]}")
                    if self.adata_path is not None:
                        signaling_type = self.signaling_types[n_feat]
                    else:
                        signaling_type = None
                    # Use each individual feature to predict the response- note y is set up as a DataFrame because in
                    # other cases column names/target names are taken from y:
                    temp_y = pd.DataFrame((y_pred_init[:, n_feat] + error).reshape(-1, 1), columns=[target])
                    temp_X = (X[:, n_feat]).reshape(-1, 1)

                    # Check if the bandwidth has plateaued for all features in this iteration:
                    if bw_plateau_counter > self.patience:
                        # Use the bandwidths from the previous iteration before plateau was determined to have been
                        # reached:
                        bw = bws[n_feat]
                        betas = self.mpi_fit(temp_y.values, temp_X, bw, final=True, multiscale=True)
                    else:
                        betas, bw_dict = self.fit(
                            temp_y,
                            temp_X,
                            init_betas=init_betas,
                            multiscale=True,
                            signaling_type=signaling_type,
                            verbose=False,
                        )
                        # Get coefficients for this particular target:
                        betas = betas[target]

                    # Update the linear predictor and betas:
                    new_v = (temp_X[indices, :] * betas).reshape(-1)
                    if self.distr != "gaussian":
                        new_y = self.distr_obj.predict(new_v)
                    else:
                        new_y = new_v
                    error = temp_y.values[indices].reshape(-1) - new_y
                    new_ys[:, n_feat] = new_y
                    new_betas[:, n_feat] = betas.reshape(-1)
                    # Update running list of bandwidths for this feature:
                    bws[n_feat] = bw_dict[target]

                # Check if ALL bandwidths remain the same between iterations:
                if (iter > 1) and np.all(bw_history[-1] == bws):
                    bw_plateau_counter += 1
                else:
                    bw_plateau_counter = 0

                # Compute normalized sum-of-squared-errors-of-prediction using the updated predicted values:
                bw_history.append(deepcopy(bws))
                numerator = np.sum((new_ys - y_pred_init) ** 2) / self.n_samples_fitted
                denominator = np.sum(np.sum(new_ys, axis=1) ** 2)
                score = (numerator / denominator) ** 0.5
                # Use the new predicted values as the initial values for the next iteration:
                y_pred_init = new_ys

                if self.comm.rank == 0:
                    self.logger.info(f"Target: {target}, Iteration: {iter}, Score: {score}")
                    self.logger.info(f"Bandwidths: {bws}")

                if score < self.tolerance:
                    self.logger.info(f"For target {target}, multiscale optimization converged after {iter} iterations.")
                    break

            # Final estimated values:
            y_pred = new_ys
            # Set dispersion for negative binomial:
            if self.distr == "nb":
                theta = 1 / self.distr_obj.variance(y_pred)
                weights = self.distr_obj.weights(y_pred)
                deviance = 2 * np.sum(
                    weights
                    * (
                        y.values[indices] * np.log(y.values[indices] / y_pred)
                        + (theta - 1) * np.log(1 + y_pred[:, 1] / (theta - 1))
                    )
                )
                dof = len(y.values[indices]) - X.shape[1]
                self.nb_disp_dict[target] = deviance / dof

            bw_history = np.array(bw_history)
            self.all_bws_history[target] = bw_history

            # Compute diagnostics for current target using the final errors:
            if self.distr == "gaussian":
                RSS = np.sum(error**2)
                self.all_RSS[target] = RSS
                # Total sum of squares:
                TSS = np.sum((y.values[indices] - np.mean(y.values[indices])) ** 2)
                self.all_TSS[target] = TSS
                r_squared = 1 - RSS / TSS

                # For saving outputs:
                header = "name,residual,"
            else:
                r_squared = None

            if self.distr == "poisson" or self.distr == "nb":
                # Deviance:
                deviance = self.distr_obj.deviance(y.values[indices], y_pred)
                self.all_deviances[target] = deviance
                residual_deviance = self.distr_obj.deviance_residuals(y.values[indices], y_pred)
                # Reshape if necessary:
                if self.n_features > 1:
                    residual_deviance = residual_deviance.reshape(-1, 1)
                self.all_residual_deviances[target] = residual_deviance

                # For saving outputs:
                header = "name,deviance,"
            else:
                deviance = None
            # Store some of the final values of interest:
            self.params_all_targets[target] = new_betas
            self.errors_all_targets[target] = error
            self.predictions_all_targets[target] = y_pred

            # Save results without standard errors or influence measures:
            if self.comm.rank == 0 and self.multiscale_params_only:
                varNames = self.feature_names
                # Save intercept and parameter estimates:
                for x in varNames:
                    header += "b_" + x + ","

                # Return output diagnostics and save result:
                self.output_diagnostics(None, None, r_squared, deviance)
                output = np.hstack([self.sample_names, error.reshape(-1, 1), self.params_all_targets[target]])
                self.save_results(header, output, label=y_label)

    def chunk_compute_metrics(
        self, X: Optional[np.ndarray] = None, chunk_id: int = 0, target_label: Optional[str] = None
    ):
        """Compute multiscale inference by chunks to reduce memory footprint- used to calculate metrics to estimate
        the importance of each feature to the variance in the data.
        Reference: Li and Fotheringham, 2020. IJGIS and Yu et al., 2019. GA.

        Args:
            X: Optional array, can be used to provide dependent variable array directly to the fit function. If
                None, will use :attr `X` computed using the given AnnData object and the type of the model to create.
                Must be the same X array as was used to fit the model (i.e. the same X given to :func
                `multiscale_backfitting`).
            chunk_id: Numerical index of the partition to be computed
            target_label: Name of the target variable to compute. Must be one of the keys of the :attr `all_bws_init`
                dictionary.

        Returns:
            ENP_chunk: Effective number of parameters for the desired chunk
            lvg_chunk: Only returned if model is a Gaussian regression model- leverage values b/w the predicted values
                and the response variable for the desired chunk
            cov_chunk: Only returned if model is a GLM- covariance matrix for the desired chunk
        """
        if X is None:
            X = self.X

        # If subsampling was run, use the subsampled indices:
        if self.subsampled:
            indices = self.indices[target_label]
        else:
            indices = np.arange(self.n_samples)

        bw = self.all_bws_init[target_label]
        bw_history = self.all_bws_history[target_label]

        chunk_size = int(np.ceil(float(self.n_fitted_target / self.n_chunks)))
        # Vector storing ENP for each predictor:
        ENP_chunk = np.zeros(self.n_features)
        # Array storing leverages for each predictor if the model is Gaussian (for each sample because of the
        # spatially-weighted nature of the regression):
        if self.distr == "gaussian":
            lvg_chunk = np.zeros((self.n_fitted_target, self.n_features))

        chunk_index = np.arange(self.n_fitted_target)[chunk_id * chunk_size : (chunk_id + 1) * chunk_size]

        # Partial hat matrix:
        init_partial_hat = np.zeros((self.n_fitted_target, len(chunk_index)))
        init_partial_hat[chunk_index, :] = np.eye(len(chunk_index))
        partial_hat = np.zeros((self.n_fitted_target, len(chunk_index), self.n_features))

        # Compute coefficients for each chunk:
        for i in indices:
            wi = get_wi(
                i, n_samples=self.n_samples, coords=self.coords, fixed_bw=self.bw_fixed, kernel=self.kernel, bw=bw
            ).reshape(-1, 1)
            xT = (X * wi).T
            # Reconstitute the response-input mapping, but only for the current chunk:
            proj = np.linalg.solve(xT.dot(X), xT).dot(init_partial_hat).T

            # Estimate the hat matrix, but only for the current chunk:
            partial_hat_i = proj * X[i]
            partial_hat[i, :, :] = partial_hat_i

        error = init_partial_hat - np.sum(partial_hat, axis=2)

        for i in range(bw_history.shape[0]):
            for j in range(self.n_features):
                proj_j_old = partial_hat[:, :, j] + error
                X_j = X[:, j]
                chunk_size_j = int(np.ceil(float(self.n_fitted_target / self.n_chunks)))
                for n in range(self.n_chunks):
                    chunk_index_temp = np.arange(self.n_fitted_target)[n * chunk_size_j : (n + 1) * chunk_size_j]
                    # Initialize response-input mapping:
                    proj_j = np.empty((len(chunk_index_temp), self.n_fitted_target))
                    # Compute the hat matrix for the current chunk:
                    for k in range(len(chunk_index_temp)):
                        # index = chunk_index_temp[k]
                        index = self.indices[chunk_index_temp[k]]
                        wi = get_wi(
                            index,
                            n_samples=self.n_samples,
                            coords=self.coords,
                            fixed_bw=self.bw_fixed,
                            kernel=self.kernel,
                            # Use the bandwidth from the ith iteration for the jth independent variable:
                            bw=bw_history[i, j],
                        ).reshape(-1)

                        xw = X_j * wi
                        proj_j[k, :] = X_j[index] / np.sum(xw * X_j) * xw

                    # Update the hat matrix:
                    partial_hat[chunk_index_temp, :, j] = proj_j.dot(proj_j_old)

                error = proj_j_old - partial_hat[:, :, j]

        # Compute leverages for each predictor (if applicable- model assumes Gaussianity), Hessian matrix (if
        # applicable- model assumes non-Gaussianity) and effective number of parameters of the model:
        for i in range(len(chunk_index)):
            ENP_chunk += partial_hat[chunk_index[i], i, :]
        if self.distr == "gaussian":
            for j in range(self.n_features):
                lvg_chunk[:, j] += ((partial_hat[:, :, j] / X[:, j].reshape(-1, 1)) ** 2).sum(axis=1)

            return ENP_chunk, lvg_chunk
        else:
            return ENP_chunk

    def multiscale_compute_metrics(self, X: Optional[np.ndarray] = None, n_chunks: int = 2):
        """Compute multiscale inference and output results.

        Args:
            X: Optional array, can be used to provide dependent variable array directly to the fit function. If
                None, will use :attr `X` computed using the given AnnData object and the type of the model to create.
                Must be the same X array as was used to fit the model (i.e. the same X given to :func
                `multiscale_backfitting`).
            n_chunks: Number of partitions comprising each covariate-specific hat matrix.
        """
        if X is None:
            X = self.X
        else:
            X = self.comm.bcast(X, root=0)

        if self.multiscale_params_only:
            self.logger.warning(
                "Chunked computations will not be performed because `multiscale_params_only` is set to True, "
                "so only parameter values (and no other metrics) will be saved."
            )
            return

        # Check that initial bandwidths and bandwidth history are present (e.g. that :func `multiscale_backfitting` has
        # been called):
        if not hasattr(self, "all_bws_history"):
            raise ValueError(
                "Initial bandwidths must be computed before calling `multiscale_fit`. Run :func "
                "`multiscale_backfitting` first."
            )

        if self.comm.rank == 0:
            self.logger.info(f"Computing model metrics, using {n_chunks} chunks...")

        self.n_chunks = self.comm.size * n_chunks
        self.chunks = np.arange(self.comm.rank * n_chunks, (self.comm.rank + 1) * n_chunks)

        y_arr = self.targets_expr if hasattr(self, "targets_expr") else self.target
        for target_label in y_arr.columns:
            sample_names = self.sample_names if not self.subsampled else self.subsampled_sample_names[target_label]
            # Fitted coefficients, errors and predictions:
            parameters = self.params_all_targets[target_label]
            errors = self.errors_all_targets[target_label]
            predictions = self.predictions_all_targets[target_label]
            y_label = target_label + "_multiscale_backfitting"

            # If subsampling was done, check for the number of fitted samples for the right target:
            if self.subsampled:
                self.n_fitted_target = self.n_samples_fitted[target_label]

            # Lists to store the results of each chunk for this variable (lvg list only used if Gaussian):
            ENP_list = []
            lvg_list = []

            for chunk in self.chunks:
                if self.distr == "gaussian":
                    ENP_chunk, lvg_chunk = self.chunk_compute_metrics(X, chunk_id=chunk, target_label=target_label)
                    ENP_list.append(ENP_chunk)
                    lvg_list.append(lvg_chunk)
                else:
                    ENP_chunk = self.chunk_compute_metrics(X, chunk_id=chunk, target_label=target_label)
                    ENP_list.append(ENP_chunk)

            # Gather results from all chunks:
            ENP_list = np.array(self.comm.gather(ENP_list, root=0))
            if self.distr == "gaussian":
                lvg_list = np.array(self.comm.gather(lvg_list, root=0))

            if self.comm.rank == 0:
                indices = np.array(sample_names).reshape(-1, 1)
                # Compile results from all chunks to get the estimated number of parameters for this response variable:
                ENP = np.sum(np.vstack(ENP_list), axis=0)

                if self.distr == "gaussian":
                    # Compile results from all chunks to get the leverage matrix for this response variable:
                    lvg = np.sum(np.vstack(lvg_list), axis=0)

                    # Get sums-of-squares corresponding to this feature:
                    RSS = self.all_RSS[target_label]
                    TSS = self.all_TSS[target_label]
                    # Residual variance:
                    sigma_squared = RSS / (self.n_fitted_target - ENP)
                    # R-squared:
                    r_squared = 1 - RSS / TSS
                    # Corrected Akaike Information Criterion:
                    aicc = self.compute_aicc_linear(RSS, ENP, n_samples=self.n_fitted_target)
                    # Scale leverages by the residual variance to compute standard errors:
                    standard_error = np.sqrt(lvg * sigma_squared)
                    self.output_diagnostics(aicc, ENP, r_squared=r_squared, deviance=None, y_label=y_label)

                    header = "index,residual,"
                    outputs = np.hstack([indices, errors.reshape(-1, 1), parameters, standard_error])

                if self.distr == "poisson" or self.distr == "nb":
                    # Get deviances corresponding to this feature:
                    deviance = self.all_deviances[target_label]
                    residual_deviance = self.all_residual_deviances[target_label]

                    # Corrected Akaike Information Criterion:
                    aicc = self.compute_aicc_glm(residual_deviance, ENP, n_samples=self.n_fitted_target)
                    # Compute standard errors using the covariance:
                    self.distr_obj.variance.disp = self.nb_disp_dict[target_label]
                    hessian = self.hessian(predictions)
                    cov_matrix = np.linalg.inv(hessian)
                    standard_error = np.sqrt(np.diag(cov_matrix))
                    self.output_diagnostics(aicc, ENP, r_squared=None, deviance=deviance, y_label=y_label)

                    header = "index,prediction,"
                    outputs = np.hstack(
                        [indices, predictions.reshape(-1, 1), parameters.reshape(-1, 1), standard_error]
                    )

                varNames = self.feature_names
                # Save intercept and parameter estimates:
                for x in varNames:
                    header += "b_" + x + ","
                for x in varNames:
                    header += "se_" + x + ","

                self.save_results(outputs, header, label=y_label)
