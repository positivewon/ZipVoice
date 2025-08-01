#!/usr/bin/env python3
# Copyright    2024-2025  Xiaomi Corp.        (authors: Wei Kang
#                                                       Han Zhu)
#
# See ../../../../LICENSE for clarification regarding multiple authors
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
"""
Usage:
      python3 -m zipvoice.bin.compute_fbank \
        --source-dir data/manifests \
        --dest-dir data/fbank \
        --dataset libritts \
        --subset dev-other \
        --sampling-rate 24000 \
        --num-jobs 20

The input would be data/manifests/libritts-cuts_dev-other.jsonl.gz or
    (libritts_supervisions_dev-other.jsonl.gz and librittsrecordings_dev-other.jsonl.gz)

The output would be data/fbank/libritts-cuts_dev-other.jsonl.gz
"""


import argparse
import logging
from concurrent.futures import ProcessPoolExecutor as Pool
from pathlib import Path

import lhotse
import torch
from lhotse import CutSet, LilcomChunkyWriter, load_manifest_lazy

from zipvoice.utils.common import str2bool
from zipvoice.utils.feature import VocosFbank

# Torch's multithreaded behavior needs to be disabled or
# it wastes a lot of CPU and slow things down.
# Do this outside of main() in case it needs to take effect
# even when we are not invoking the main (e.g. when spawning subprocesses).
torch.set_num_threads(1)
torch.set_num_interop_threads(1)

lhotse.set_audio_duration_mismatch_tolerance(0.1)


def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--sampling-rate",
        type=int,
        default=24000,
        help="The target sampling rate, the audio will be resampled to it.",
    )

    parser.add_argument(
        "--type",
        type=str,
        default="vocos",
        help="fbank type",
    )

    parser.add_argument(
        "--dataset",
        type=str,
        help="Dataset name.",
    )

    parser.add_argument(
        "--subset",
        type=str,
        help="The subset of the dataset.",
    )

    parser.add_argument(
        "--source-dir",
        type=str,
        default="data/manifests",
        help="The source directory of manifest files.",
    )

    parser.add_argument(
        "--dest-dir",
        type=str,
        default="data/fbank",
        help="The destination directory of manifest files.",
    )

    parser.add_argument(
        "--split-cuts",
        type=str2bool,
        default=False,
        help="Whether to use splited cuts.",
    )

    parser.add_argument(
        "--split-begin",
        type=int,
        help="Start idx of splited cuts.",
    )

    parser.add_argument(
        "--split-end",
        type=int,
        help="End idx of splited cuts.",
    )

    parser.add_argument(
        "--batch-duration",
        type=int,
        default=1000,
        help="The batch duration when computing the features.",
    )

    parser.add_argument(
        "--num-jobs",
        type=int,
        default=20,
        help="The number of extractor workers.",
    )

    return parser.parse_args()


def compute_fbank_split_single(params, idx):
    logging.info(
        f"Computing features for {idx}-th split of "
        f"{params.dataset} dataset {params.subset} subset"
    )
    lhotse.set_audio_duration_mismatch_tolerance(0.1)  # for emilia
    src_dir = Path(params.source_dir)
    output_dir = Path(params.dest_dir)

    if not src_dir.exists():
        logging.error(f"{src_dir} not exists")
        return

    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    num_digits = 8
    if params.type == "vocos":
        extractor = VocosFbank()
    else:
        raise NotImplementedError(f"{params.type} is not supported")

    prefix = params.dataset
    subset = params.subset
    suffix = "jsonl.gz"

    idx = f"{idx}".zfill(num_digits)
    cuts_filename = f"{prefix}_cuts_{subset}.{idx}.{suffix}"

    if (src_dir / cuts_filename).is_file():
        logging.info(f"Loading manifests {src_dir / cuts_filename}")
        cut_set = load_manifest_lazy(src_dir / cuts_filename)
    else:
        logging.warning(f"Raw {cuts_filename} not exists, skipping")
        return

    cut_set = cut_set.resample(params.sampling_rate)

    if (output_dir / cuts_filename).is_file():
        logging.info(f"{cuts_filename} already exists - skipping.")
        return

    logging.info(f"Processing {subset}.{idx} of {prefix}")

    cut_set = cut_set.compute_and_store_features_batch(
        extractor=extractor,
        storage_path=f"{output_dir}/{prefix}_feats_{subset}_{idx}",
        num_workers=4,
        batch_duration=params.batch_duration,
        storage_type=LilcomChunkyWriter,
        overwrite=True,
    )
    logging.info(f"Saving file to {output_dir / cuts_filename}")
    cut_set.to_file(output_dir / cuts_filename)


def compute_fbank_split(params):
    if params.split_end < params.split_begin:
        logging.warning(
            f"Split begin should be smaller than split end, given "
            f"{params.split_begin} -> {params.split_end}."
        )

    with Pool(max_workers=params.num_jobs) as pool:
        futures = [
            pool.submit(compute_fbank_split_single, params, i)
            for i in range(params.split_begin, params.split_end)
        ]
        for f in futures:
            f.result()
            f.done()


def compute_fbank(params):
    logging.info(
        f"Computing features for {params.dataset} dataset {params.subset} subset"
    )
    src_dir = Path(params.source_dir)
    output_dir = Path(params.dest_dir)
    num_jobs = params.num_jobs
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    prefix = params.dataset
    subset = params.subset
    suffix = "jsonl.gz"

    cut_set_name = f"{prefix}_cuts_{subset}.{suffix}"

    if (src_dir / cut_set_name).is_file():
        logging.info(f"Loading manifests {src_dir / cut_set_name}")
        cut_set = load_manifest_lazy(src_dir / cut_set_name)
    else:
        recordings = load_manifest_lazy(
            src_dir / f"{prefix}_recordings_{subset}.{suffix}"
        )
        supervisions = load_manifest_lazy(
            src_dir / f"{prefix}_supervisions_{subset}.{suffix}"
        )
        cut_set = CutSet.from_manifests(
            recordings=recordings,
            supervisions=supervisions,
        )

    cut_set = cut_set.resample(params.sampling_rate)
    if params.type == "vocos":
        extractor = VocosFbank()
    else:
        raise NotImplementedError(f"{params.type} is not supported")

    cuts_filename = f"{prefix}_cuts_{subset}.{suffix}"
    if (output_dir / cuts_filename).is_file():
        logging.info(f"{prefix} {subset} already exists - skipping.")
        return
    logging.info(f"Processing {subset} of {prefix}")

    cut_set = cut_set.compute_and_store_features(
        extractor=extractor,
        storage_path=f"{output_dir}/{prefix}_feats_{subset}",
        num_jobs=num_jobs,
        storage_type=LilcomChunkyWriter,
    )
    logging.info(f"Saving file to {output_dir / cuts_filename}")
    cut_set.to_file(output_dir / cuts_filename)


if __name__ == "__main__":
    formatter = "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    logging.basicConfig(format=formatter, level=logging.INFO, force=True)

    args = get_args()
    logging.info(vars(args))
    if args.split_cuts:
        compute_fbank_split(params=args)
    else:
        compute_fbank(params=args)
    logging.info("Done!")
