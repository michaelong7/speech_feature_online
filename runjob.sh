#! /usr/bin/env bash

set -euo pipefail

# Simple script to test shennong analysis process locally
# Pass in path to audio file you wish to analyze
# Will place config file in /tmp directory for storage before uploading

if [[ $# -ne 1 ]]; then
    echo -e >&2 "USAGE: $0 
        /path/to/audio/file.wav" && exit 1
fi

if [[ ! -f "${1}" ]]; then
    echo >&2 "${1} does not exist!" && exit 1
fi

file_path="${1}"

# pull credentials from project .env file
aws_default_region=$( cat .env | awk -F= '/AWS_DEFAULT_REGION/ {print $2}')
aws_secret_access_key=$( cat .env | awk -F= '/AWS_SECRET_ACCESS_KEY/ {print $2}')
aws_access_key_id=$( cat .env | awk -F= '/AWS_ACCESS_KEY_ID/ {print $2}')
bucket_name=$( cat .env | awk -F= '/BUCKET_NAME/ {print $2}')

#sample analysis config
config_json=$(cat <<EOF
{
    "analyses": {
        "bottleneck": {
            "init_args": {
                "weights": "BabelMulti",
                "dither": 0.1
            },
            "postprocessors": []
        }
    },
    "channel": 1,
    "files": [],
    "res": ".npz",
    "save_path": "test/not-random-123.zip"
}
EOF
)

config_key="test-config.json"
file_key="$(basename "${file_path}")"

# put audio file in bucket
docker-compose run --no-deps -v "${file_path}:/tmp/"${file_key}":ro" --entrypoint="python /code/scripts/upload_file.py /tmp/"${file_key}" "${file_key}""  worker

# add audio file path to config and store in host tmp dir
echo $config_json | jq --arg file_key $file_key '.files +=  [$file_key]' > /tmp/${config_key}

# put config in bucket
docker-compose run --no-deps -v "/tmp/${config_key}:/tmp/${config_key}:ro" --entrypoint="python /code/scripts/upload_file.py /tmp/${config_key} ${config_key}" worker

# argument passed to run script contains config path in s3 and bucket name so it can be retrieved by shennong runner
run_arg_json=$(cat <<EOF
{
    "bucket": "${bucket_name}",
    "config_path": "${config_key}"
}
EOF
)

image="ghcr.io/perceptimatic/sfo-shennong-runner:latest"

# run the job
docker run -it -e "AWS_DEFAULT_REGION=${aws_default_region}" -e "AWS_SECRET_ACCESS_KEY=${aws_secret_access_key}" -e "AWS_ACCESS_KEY_ID=${aws_access_key_id}" --rm "${image}" "${run_arg_json}"
