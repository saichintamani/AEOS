output "artifacts_bucket" {
  description = "Artifacts S3 bucket name"
  value       = aws_s3_bucket.artifacts.bucket
}

output "artifacts_bucket_arn" {
  description = "Artifacts S3 bucket ARN"
  value       = aws_s3_bucket.artifacts.arn
}

output "backups_bucket" {
  description = "Backups S3 bucket name"
  value       = aws_s3_bucket.backups.bucket
}

output "backups_bucket_arn" {
  description = "Backups S3 bucket ARN"
  value       = aws_s3_bucket.backups.arn
}

output "tfstate_bucket" {
  description = "Terraform state S3 bucket name (if created)"
  value       = var.create_tfstate_bucket ? aws_s3_bucket.tfstate[0].bucket : null
}

output "tflock_table" {
  description = "DynamoDB table name for Terraform state locking (if created)"
  value       = var.create_tfstate_bucket ? aws_dynamodb_table.tflock[0].name : null
}

output "kms_key_arn" {
  description = "KMS key ARN used for S3 encryption"
  value       = aws_kms_key.s3.arn
}
