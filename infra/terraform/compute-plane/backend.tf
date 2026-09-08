# Remote state, deliberately in a bucket Terraform does not manage.
#
# WHY THIS BUCKET IS NOT IN ANY .tf FILE. A state bucket described by the
# configuration whose state it holds is circular: destroying the layer would
# destroy the record of what was destroyed, mid-destroy. It is created once by
# hand (versioning, SSE, public access blocked, 90 days of version history) and
# left alone. scripts/bootstrap_state_bucket.sh records exactly how.
#
# WHY IT WAS WORTH DOING AT ALL. The state held both Neon passwords in
# cleartext and existed on precisely one laptop. Losing that laptop meant
# losing the ability to manage OR destroy a running stack — Terraform would no
# longer know any resource existed, `terraform destroy` would report nothing to
# do, and every resource would keep billing with no inventory of what to delete
# by hand. It is now versioned, encrypted at rest, and off the machine.
#
# use_lockfile is S3-native locking (Terraform >= 1.10). No DynamoDB table, so
# no extra bill and nothing else to remember to destroy.
terraform {
  backend "s3" {
    bucket       = "docfactory-tfstate-215472107457"
    key          = "compute-plane/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}
