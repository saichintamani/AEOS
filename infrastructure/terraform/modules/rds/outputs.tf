output "endpoint" {
  description = "RDS primary endpoint (write)"
  value       = aws_db_instance.main.endpoint
}

output "replica_endpoint" {
  description = "RDS read replica endpoint (if created)"
  value       = var.create_replica ? aws_db_instance.replica[0].endpoint : null
}

output "port" {
  description = "RDS port"
  value       = aws_db_instance.main.port
}

output "database_name" {
  description = "Database name"
  value       = aws_db_instance.main.db_name
}

output "security_group_id" {
  description = "Security group ID of the RDS instance"
  value       = aws_security_group.rds.id
}

output "instance_id" {
  description = "RDS instance identifier"
  value       = aws_db_instance.main.id
}
