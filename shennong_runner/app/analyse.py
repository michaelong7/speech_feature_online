from contextlib import AbstractContextManager
from dataclasses import dataclass
from importlib import import_module
from json import load, loads
import logging
from os import mkdir, path
from posixpath import basename
from shutil import make_archive, rmtree
import tempfile
from typing import Any, Dict, List
import uuid

import boto3

from shennong import FeaturesCollection
from shennong.audio import Audio

# this is here to prevent a circular dependency
from shennong.processor.pitch_kaldi import KaldiPitchPostProcessor
from shennong.postprocessor.cmvn import CmvnPostProcessor

from app.settings import settings as app_settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
ch = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(message)s")
ch.setFormatter(formatter)
logger.addHandler(ch)


with open(path.join(app_settings.PROJECT_ROOT, "processor-schema.json")) as f:
    shennong_schema = loads(f.read())


def resolve_processor(class_key: str, init_args: Dict[str, Any]):
    class_name = shennong_schema["processors"][class_key]["class_name"]
    module = import_module(f"shennong.processor.{class_key}")
    cls = module.__dict__[class_name]
    return cls(**init_args)


@dataclass
class JobArgs:
    bucket: str
    config_path: str


@dataclass
class JobConfig:
    analyses: Dict[str, Any]
    channel: int
    files: List[str]
    save_path: str
    res: str


class CmvnWrapper:
    """A wrapper for this postprocessor, so that it implements our simplified API"""

    def __init__(self, ndims: int):
        self.processor = CmvnPostProcessor(ndims)

    def process(self, features):
        self.processor.accumulate(features)
        return self.processor.process(features)


def resolve_postprocessor(class_key: str, features=None):
    if class_key == "cmvn":
        return CmvnWrapper(features.ndims)
    class_name = shennong_schema["postprocessors"][class_key]["class_name"]
    try:
        module = import_module(f"shennong.postprocessor.{class_key}")
        return module.__dict__[class_name]()
    except ImportError:
        module = import_module(f"shennong.processor.{class_key}")
        return module.__dict__[class_name]()


class Analyser:
    """Resolve processors and postprocessors from config and run analyses"""

    def __init__(
        self, filepath: str, channel: int, collection: FeaturesCollection,
    ):
        self.collection = collection
        sound = Audio.load(filepath)
        if (
            sound.nchannels > 1
        ):  # converting to mono; user-set or default channel chosen:
            sound = sound.channel(channel - 1)
        self.sound = sound

    def postprocess(self, postprocessor: str, processor_type: str):
        postprocessor = resolve_postprocessor(
            postprocessor, self.collection[processor_type]
        )
        return postprocessor.process(self.collection[processor_type])

    def process(self, key: str, settings: Dict[str, Any]):
        postprocessors = settings["postprocessors"] or []
        if settings["init_args"].get("sample_rate"):
            settings["init_args"]["sample_rate"] = self.sound.sample_rate
        processor = resolve_processor(key, settings["init_args"])
        self.collection[key] = processor.process(self.sound)
        if postprocessors:
            for pp in postprocessors:
                pp_key = f"{key}_{pp}"
                logger.info(f"starting {pp_key} postprocessor")
                self.collection[pp_key] = self.postprocess(pp, key)
                logger.info(f"finished {pp_key} postprocessor")


class LocalFileManager(AbstractContextManager):
    """Local filesystem provider.
    Handles temporary file storage and, if we're not using s3, result storage.
    """

    def __init__(self, tmp_dir: str = None):
        """Where to save results when storing analyses on the container's filesystem (typically dev env).
        Normally tmp_dir can be ignored; it is used mostly for testing.
        """

        """ Root dir for temp files, to be removed atexit """
        self.tmp_dir = (
            tmp_dir if tmp_dir else path.join(tempfile.gettempdir(), uuid.uuid4().hex)
        )
        mkdir(self.tmp_dir)
        """ Where to store results before zipping"""
        self.tmp_results_dir = path.join(self.tmp_dir, uuid.uuid4().hex)
        """ Where to store downloads before processing """
        self.tmp_download_dir = path.join(self.tmp_dir, uuid.uuid4().hex)
        mkdir(self.tmp_results_dir)
        mkdir(self.tmp_download_dir)

    def __exit__(self, exc_type, exc_value, traceback):
        # temps on filesystem no longer matter b/c node is terminated at job end
        # leaving base file on S3 so that users can rerun jobs for 7 days
        # self.remove_temps()
        pass

    def get_tmp_result_dir_name(self, name: str):
        """Create a temporary directory on the local filesystem"""
        dirpath = path.join(self.tmp_results_dir, name)
        return dirpath

    def get_tmp_result_path(self, filepath: str, extension: str):
        """Return the name of a valid filepath for an intermediate file"""
        result_path = path.join(
            self.tmp_results_dir,
            f"{path.splitext(path.basename(filepath))[0]}-features{extension}",
        )
        return result_path

    def remove_temps(self):
        """Remove directory and contents from registered temp files"""

        def log_error(function, path, excinfo):
            logger.info(excinfo)

        rmtree(self.tmp_dir, onerror=log_error)

        return True

    def zip_tmp_files(self):
        """Zip intermediate files"""
        return make_archive(
            path.join(self.tmp_dir, "sfo-results"), "zip", self.tmp_results_dir,
        )


class S3FileManager(LocalFileManager):
    """S3 client provider that employs and overrides local methods as necessary."""

    def __init__(self, bucket_name: str):
        super().__init__()
        self.resource = boto3.resource("s3")
        self.client = boto3.client("s3")
        self.bucket = bucket_name

    def load(self, key):
        """Download file from s3 and store both key and local temp path for cleanup"""
        basename = path.basename(key)
        save_path = path.join(self.tmp_download_dir, basename)
        self.resource.Bucket(self.bucket).download_file(key, save_path)
        return save_path

    def store(self, save_path: str):
        """Zip up results, upload to bucket, and queue local zip file for removal"""
        zip_path = self.zip_tmp_files()
        self.resource.meta.client.upload_file(zip_path, self.bucket, save_path)
        return True


def process_data(job_args: JobArgs,):
    """Process each file passed for analysis"""

    storage_manager = S3FileManager(job_args.bucket)

    config_path = storage_manager.load(job_args.config_path)

    with open(config_path) as f:
        jobconfig = JobConfig(**load(f))

    file_paths = jobconfig.files
    res_type = jobconfig.res
    channel = jobconfig.channel
    analysis_settings = jobconfig.analyses

    with storage_manager as manager:
        # shennong the devil outta them:
        for file_path in file_paths:

            collection = FeaturesCollection()
            local_path = manager.load(file_path)
            analyser = Analyser(local_path, channel, collection)
            logger.info(f"starting {file_path}")

            for k, v in analysis_settings.items():
                logger.info(f"starting {k}")
                analyser.process(k, v)
                logger.info(f"finished {k}")
            """ csv serializers save a csv and a json file,
                so they must be passed a directory path rather than a file path
                https://github.com/bootphon/shennong/blob/master/shennong/serializers.py#L35  
            """
            if res_type == ".csv":
                serializer = "csv"
                bname = basename(file_path)
                outpath = manager.get_tmp_result_dir_name(bname)
            else:
                # if not a csv, let shennong resolve the serializer
                serializer = None
                outpath = manager.get_tmp_result_path(local_path, res_type)
            analyser.collection.save(outpath, serializer=serializer)
            logger.info(f"saved {file_path}")

        manager.store(jobconfig.save_path)

    return True


if __name__ == "__main__":
    from sys import argv

    args = loads(argv[1])
    process_data(JobArgs(**args))
