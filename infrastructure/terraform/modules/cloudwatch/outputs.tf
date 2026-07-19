output "api_log_group" {
  description = "CloudWatch log group name for API"
  value       = aws_cloudwatch_log_group.api.name
}

output "worker_log_group" {
  description = "CloudWatch log group name for worker"
  value       = aws_cloudwatch_log_group.worker.name
}

output "dashboard_url" {
  description = "CloudWatch dashboard URL"
  value       = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home#dashboards:name=${aws_cloudwatch_dashboard.aeos.dashboard_name}"
}
