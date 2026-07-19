output "irsa_role_arns" {
  description = "Map of service account key → IAM role ARN"
  value       = { for k, v in aws_iam_role.irsa : k => v.arn }
}

output "api_role_arn" {
  description = "IAM role ARN for the API service account"
  value       = aws_iam_role.irsa["api"].arn
}

output "worker_role_arn" {
  description = "IAM role ARN for the worker service account"
  value       = aws_iam_role.irsa["worker"].arn
}

output "eso_role_arn" {
  description = "IAM role ARN for External Secrets Operator"
  value       = aws_iam_role.irsa["eso"].arn
}
