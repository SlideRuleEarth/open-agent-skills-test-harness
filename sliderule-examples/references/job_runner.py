# --- Running SlideRule Jobs (Batch Processing) ---
#
# This notebook provides example code for submitting custom lua scripts to be
# executed by the **SlideRule Runner**, which is SlideRule's batch job
# processing environment

# --- Imports ---

from sliderule import sliderule
from datetime import datetime
import boto3
import json
import time

# --- Initialize *SlideRule* session ---

session = sliderule.create_session(verbose=True)
session.authenticate() # gives privileges to access SlideRule Runner

# --- Submit user Lua script to *SlideRule Runner* ---

lua_script = """
print("Hello World")
return "Nice to meet you", true
"""

rsps = session.runner.submit(name="hello_world", script=lua_script, args=[" "])
rsps

# --- Display status for *SlideRule Runner* ---

# Display status for the specific job that was just submitted
job_id = rsps['job_ids'][0]
job_status = session.runner.jobs(job_list=[job_id])
print(json.dumps(job_status, indent=2))

# Display status for jobs that are still in the process of being run
jobs_in_progress = session.runner.queue(job_state=["SUBMITTED", "PENDING", "RUNNABLE", "STARTING", "RUNNING"])
print(json.dumps(jobs_in_progress, indent=2))

# Display status for jobs that have finished
jobs_finished = session.runner.queue(job_state=["SUCCEEDED", "FAILED"])
print(json.dumps(jobs_finished, indent=2))

# --- Wait for job to complete ---

job_status = session.runner.jobs(job_list=[job_id])
while job_status[job_id]["status"] not in ["SUCCEEDED", "FAILED"]:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{now} - waiting for job {job_id} to complete, currently {job_status[job_id]["status"]}")
    time.sleep(30)
    job_status = session.runner.jobs(job_list=[job_id])
print(job_status)

# --- Read results from S3 ---

s3 = boto3.client("s3", region_name="us-west-2")

# list contents of an s3 bucket
def list_bucket(url):
    filenames = []
    bucket = url.split("s3://")[-1].split("/")[0]
    prefix = "/".join(url.split("s3://")[-1].split("/")[1:])
    is_truncated = True
    continuation_token = None
    while is_truncated:
        # make request
        if continuation_token:
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, ContinuationToken=continuation_token)
        else:
            response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
        # parse contents
        if 'Contents' in response:
            for obj in response['Contents']:
                filenames.append(f"{bucket}/{obj['Key']}")
        # check if more data is available
        is_truncated = response['IsTruncated']
        continuation_token = response.get('NextContinuationToken')
    return filenames

# Download and display run artifacts
filenames = list_bucket(rsps["run_url"])
for filename in filenames:
    bucket = filename.split("/")[0]
    key = "/".join(filename.split("/")[1:])
    local_file = f"/tmp/{filename.split("/")[-1]}"

    print(f"\nDownloading s3://{filename} to {local_file}")
    s3.download_file(bucket, key, local_file)

    print(f"Contents of {local_file}:")
    with open(local_file, "r") as file:
        contents = file.read()
        print(contents)
