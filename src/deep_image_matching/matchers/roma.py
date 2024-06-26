import shutil
from pathlib import Path

import cv2
import h5py
import numpy as np
import torch
from PIL import Image

from .. import TileSelection, Timer, logger
from ..io.h5 import get_features
from ..thirdparty.RoMa.roma import roma_outdoor
from ..utils.geometric_verification import geometric_verification
from ..utils.tiling import Tiler
from .matcher_base import DetectorFreeMatcherBase, tile_selection


class RomaMatcher(DetectorFreeMatcherBase):
    """
    RomaMatcher class for feature matching using RoMa.

    Attributes:
        default_conf (dict): Default configuration options.
        max_feat_no_tiling (int): Maximum number of features when tiling is not used.
        grayscale (bool): Flag indicating whether images are processed in grayscale.
        as_float (bool): Flag indicating whether to use float for image pixel values.

    Methods:
        __init__: Constructor.
        match(self, feature_path: Path, matches_path: Path, img0: Path, img1: Path, try_full_image: bool = False) -> np.ndarray: Match features between two images.
    """

    grayscale = False
    as_float = True
    max_tile_size = 448
    max_tile_pairs = 50
    keep_tiles = False

    def __init__(self, config={}) -> None:
        super().__init__(config)

        self.matcher = roma_outdoor(device=self._device)

        logger.warning(
            "RoMa uses a fixed tile size of 448x448 pixels. This can result in an enormous amount of tiles with high resolution images. If this is your case, try to downscale images using a lower 'Quality' value."
        )
        self._config["general"]["tile_size"] = (self.max_tile_size, self.max_tile_size)

    def match(
        self,
        feature_path: Path,
        matches_path: Path,
        img0: Path,
        img1: Path,
        try_full_image: bool = False,
    ):
        """
        Match features between two images.

        Args:
            feature_path (Path): Path to the feature file.
            matches_path (Path): Path to save the matches.
            img0 (Path): Path to the first image.
            img1 (Path): Path to the second image.
            try_full_image (bool, optional): Flag to attempt matching on full images. Defaults to False.

        Raises:
            RuntimeError: If there are too many features to match on full images.

        Returns:
            np.ndarray: Array containing the indices of matched keypoints.
        """
        timer_match = Timer(log_level="debug")

        # Check that feature_path exists
        if not Path(feature_path).exists():
            raise FileNotFoundError(f"Feature file {feature_path} does not exist.")
        else:
            self._feature_path = Path(feature_path)

        # Get features from h5 file
        img0 = Path(img0)
        img1 = Path(img1)
        img0_name = img0.name
        img1_name = img1.name

        # Perform matching
        if self._tiling == TileSelection.NONE:
            matches = self._match_pairs(self._feature_path, img0, img1)
            timer_match.update("[match] Match full images")
        else:
            matches = self._match_by_tile(
                feature_path,
                img0,
                img1,
                method=self._tiling,
                select_unique=True,
            )
            timer_match.update("[match] Match by tile")

        # Do Geometric verification
        features0 = get_features(feature_path, img0_name)
        features1 = get_features(feature_path, img1_name)

        # Rescale threshold according the image original image size
        img_shape = cv2.imread(str(img0)).shape
        scale_fct = np.floor(max(img_shape) / self.max_tile_size / 2)
        gv_threshold = self._config["general"]["gv_threshold"] * scale_fct

        # Apply geometric verification
        _, inlMask = geometric_verification(
            kpts0=features0["keypoints"][matches[:, 0]],
            kpts1=features1["keypoints"][matches[:, 1]],
            method=self._config["general"]["geom_verification"],
            threshold=gv_threshold,
            confidence=self._config["general"]["gv_confidence"],
        )
        matches = matches[inlMask]
        timer_match.update("Geom. verification")

        # Save to h5 file
        n_matches = len(matches)
        with h5py.File(str(matches_path), "a", libver="latest") as fd:
            group = fd.require_group(img0_name)
            if n_matches >= self.min_inliers_per_pair:
                group.create_dataset(img1_name, data=matches)
            else:
                logger.debug(
                    f"Too few matches found. Skipping image pair {img0.name}-{img1.name}"
                )
                return None
        timer_match.update("save to h5")

        timer_match.print(f"{__class__.__name__} match")

    @torch.no_grad()
    def _match_pairs(
        self,
        feature_path: Path,
        img0_path: Path,
        img1_path: Path,
    ):
        """
        Perform matching between feature pairs.

        Args:
            feature_path (Path): Path to the feature file.
            img0_path (Path): Path to the first image.
            img1_path (Path): Path to the second image.

        Returns:
            np.ndarray: Array containing the indices of matched keypoints.
        """

        img0_name = img0_path.name
        img1_name = img1_path.name

        # Run inference
        with torch.inference_mode():
            # /home/luca/Desktop/gitprojects/github_3dom/deep-image-matching/src/deep_image_matching/thirdparty/RoMa/roma/models/matcher.py
            # in class RegressionMatcher(nn.Module) def __init__ hardcoded self.upsample_res = (int(864/4), int(864/4))
            W_A, H_A = Image.open(img0_path).size
            W_B, H_B = Image.open(img1_path).size

            warp, certainty = self.matcher.match(
                str(img0_path), str(img1_path), device=self._device, batched=False
            )
            matches, certainty = self.matcher.sample(warp, certainty)
            kptsA, kptsB = self.matcher.to_pixel_coordinates(
                matches, H_A, W_A, H_B, W_B
            )
            kptsA, kptsB = kptsA.cpu().numpy(), kptsB.cpu().numpy()

        # Create a 1-to-1 matching array
        matches0 = np.arange(kptsA.shape[0])
        matches = np.hstack((matches0.reshape((-1, 1)), matches0.reshape((-1, 1))))
        self._update_features_h5(
            feature_path,
            img0_name,
            img1_name,
            kptsA,
            kptsB,
            matches,
        )

        return matches

    def _match_by_tile(
        self,
        feature_path: Path,
        img0: Path,
        img1: Path,
        method: TileSelection = TileSelection.PRESELECTION,
        select_unique: bool = True,
    ) -> np.ndarray:
        """
        Perform matching by tile.

        Args:
            feature_path (Path): Path to the feature file.
            img0 (Path): Path to the first image.
            img1 (Path): Path to the second image.
            method (TileSelection, optional): Tile selection method. Defaults to TileSelection.PRESELECTION.
            select_unique (bool, optional): Flag to select unique features. Defaults to True.

        Returns:
            np.ndarray: Array containing the indices of matched keypoints.
        """

        def write_tiles_disk(output_dir: Path, tiles: dict) -> None:
            output_dir = Path(output_dir)
            if output_dir.exists():
                return None
            output_dir.mkdir(parents=True)
            for i, tile in tiles.items():
                name = str(output_dir / f"tile_{i}.png")
                cv2.imwrite(name, tile)

        timer = Timer(log_level="debug", cumulate_by_key=True)

        tile_size = self._config["general"]["tile_size"]
        overlap = self._config["general"]["tile_overlap"]
        img0_name = img0.name
        img1_name = img1.name

        # Select tile pairs to match
        tile_pairs = tile_selection(
            img0,
            img1,
            method=method,
            quality=self._quality,
            preselction_extractor=self._preselction_extractor,
            preselction_matcher=self._preselction_matcher,
            tile_size=tile_size,
            tile_overlap=overlap,
            tile_preselection_size=self.tile_preselection_size,
            min_matches_per_tile=self.min_matches_per_tile,
            device=self._device,
        )
        if len(tile_pairs) > self.max_tile_pairs:
            raise RuntimeError(
                f"Too many tile pairs ({len(tile_pairs)}) to match, the matching process will be too slow and it may be inaccurate. Try to reduce the image resolution using a lower 'Quality' value (or change the 'max_tile_pairs' class attribute, if you know what you are doing)."
            )

        timer.update("tile selection")

        # Read images and resize them if needed
        image0 = cv2.imread(str(img0))
        image1 = cv2.imread(str(img1))
        image0.shape[:2]
        image1.shape[:2]
        image0 = self._resize_image(self._quality, image0)
        image1 = self._resize_image(self._quality, image1)

        # If tiling is used, extract tiles with proper size for RoMa matching and save them to disk
        tiler = Tiler(tiling_mode="size")
        tiles0, t_origins0, t_padding0 = tiler.compute_tiles_by_size(
            input=image0, window_size=tile_size, overlap=overlap
        )
        tiles1, t_origins1, t_padding1 = tiler.compute_tiles_by_size(
            input=image1, window_size=tile_size, overlap=overlap
        )
        tiles_dir = Path(self._config["general"]["output_dir"]) / "tiles"
        write_tiles_disk(tiles_dir / img0.name, tiles0)
        write_tiles_disk(tiles_dir / img1.name, tiles1)
        logger.debug(f"Tiles saved to {tiles_dir}")

        # Match each tile pair
        mkpts0_full = np.array([], dtype=np.float32).reshape(0, 2)
        mkpts1_full = np.array([], dtype=np.float32).reshape(0, 2)
        conf_full = np.array([], dtype=np.float32)

        for tidx0, tidx1 in tile_pairs:
            logger.debug(f"  - Matching tile pair ({tidx0}, {tidx1})")

            tile_path0 = tiles_dir / img0.name / f"tile_{tidx0}.png"
            tile_path1 = tiles_dir / img1.name / f"tile_{tidx1}.png"

            W_A, H_A = tiles0[tidx0].shape[1], tiles0[tidx0].shape[0]
            W_B, H_B = tiles1[tidx1].shape[1], tiles1[tidx1].shape[0]

            # Run inference
            with torch.inference_mode():
                warp, certainty = self.matcher.match(
                    str(tile_path0), str(tile_path1), device=self._device, batched=False
                )
                matches, certainty = self.matcher.sample(warp, certainty)
                kptsA, kptsB = self.matcher.to_pixel_coordinates(
                    matches, H_A, W_A, H_B, W_B
                )
                kptsA, kptsB = kptsA.cpu().numpy(), kptsB.cpu().numpy()

                # Get match confidence
                conf = certainty.cpu().numpy()

            # For debugging
            # from ..visualization import viz_matches_cv2

            # viz_matches_cv2(
            #     tiles0[tidx0],
            #     tiles1[tidx1],
            #     kptsA,
            #     kptsB,
            #     f"sandbox/tile_{tidx0}-{tidx1}.png",
            #     line_thickness=-1,
            #     autoresize=False,
            #     jpg_quality=80,
            # )

            # Restore original image coordinates (not cropped)
            kptsA = kptsA + np.array(t_origins0[tidx0]).astype("float32")
            kptsB = kptsB + np.array(t_origins1[tidx1]).astype("float32")

            # Check if any keypoints are outside the original image (non-padded) or too close to the border
            def kps_in_image(kp, img_size, border_thr=2):
                return (
                    (kp[:, 0] >= border_thr)
                    & (kp[:, 0] < img_size[1] - border_thr)
                    & (kp[:, 1] >= border_thr)
                    & (kp[:, 1] < img_size[0] - border_thr)
                )

            maskA = kps_in_image(kptsA, image0.shape[:2])
            maskB = kps_in_image(kptsB, image1.shape[:2])
            msk = maskA & maskB
            kptsA = kptsA[msk]
            kptsB = kptsB[msk]

            # Append to full arrays
            mkpts0_full = np.vstack((mkpts0_full, kptsA))
            mkpts1_full = np.vstack((mkpts1_full, kptsB))
            conf_full = np.concatenate((conf_full, conf))

        # Rescale keypoints to original image size
        mkpts0_full = self._resize_keypoints(self._quality, mkpts0_full)
        mkpts1_full = self._resize_keypoints(self._quality, mkpts1_full)

        # Select uniue features on image 0, on rounded coordinates
        if select_unique is True:
            decimals = 1
            _, unique_idx = np.unique(
                np.round(mkpts0_full, decimals), axis=0, return_index=True
            )
            mkpts0_full = mkpts0_full[unique_idx]
            mkpts1_full = mkpts1_full[unique_idx]

        # Create a 1-to-1 matching array
        matches0 = np.arange(mkpts0_full.shape[0])
        matches = np.hstack((matches0.reshape((-1, 1)), matches0.reshape((-1, 1))))
        matches = self._update_features_h5(
            feature_path,
            img0_name,
            img1_name,
            mkpts0_full,
            mkpts1_full,
            matches,
        )

        # Remove tiles from disk
        if not self.keep_tiles:
            shutil.rmtree(tiles_dir)

        return matches
