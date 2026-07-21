## CloudWatch Module — dashboards, alarms, and log groups for AEOS

locals {
  alarm_actions = compact([var.sns_topic_arn])
}

## ─── Log Groups ───────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aeos/${var.environment}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aeos/${var.environment}/worker"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

resource "aws_cloudwatch_log_group" "scheduler" {
  name              = "/aeos/${var.environment}/scheduler"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

## ─── API Alarms ───────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "api_error_rate" {
  alarm_name          = "${var.prefix}-api-error-rate"
  alarm_description   = "API 5xx error rate > 1%"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 1
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "error_rate"
    expression  = "errors / total * 100"
    label       = "Error Rate (%)"
    return_data = true
  }

  metric_query {
    id = "errors"
    metric {
      namespace   = "AEOS"
      metric_name = "http_requests_total"
      period      = 60
      stat        = "Sum"
      dimensions  = { status_class = "5xx", environment = var.environment }
    }
  }

  metric_query {
    id = "total"
    metric {
      namespace   = "AEOS"
      metric_name = "http_requests_total"
      period      = 60
      stat        = "Sum"
      dimensions  = { environment = var.environment }
    }
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "api_p99_latency" {
  alarm_name          = "${var.prefix}-api-p99-latency"
  alarm_description   = "API P99 latency > 5s"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 5000
  treat_missing_data  = "notBreaching"

  namespace   = "AEOS"
  metric_name = "http_request_duration_p99"
  period      = 60
  statistic   = "Maximum"
  dimensions  = { environment = var.environment }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

## ─── Worker Alarms ────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "task_failure_rate" {
  alarm_name          = "${var.prefix}-task-failure-rate"
  alarm_description   = "Task failure rate > 5%"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 5
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "failure_rate"
    expression  = "failures / total * 100"
    label       = "Failure Rate (%)"
    return_data = true
  }

  metric_query {
    id = "failures"
    metric {
      namespace   = "AEOS"
      metric_name = "task_executions_total"
      period      = 60
      stat        = "Sum"
      dimensions  = { status = "failed", environment = var.environment }
    }
  }

  metric_query {
    id = "total"
    metric {
      namespace   = "AEOS"
      metric_name = "task_executions_total"
      period      = 60
      stat        = "Sum"
      dimensions  = { environment = var.environment }
    }
  }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "worker_queue_depth" {
  alarm_name          = "${var.prefix}-worker-queue-depth"
  alarm_description   = "Worker queue depth > 1000 for 5 minutes"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 5
  threshold           = 1000
  treat_missing_data  = "notBreaching"

  namespace   = "AEOS"
  metric_name = "worker_queue_depth"
  period      = 60
  statistic   = "Maximum"
  dimensions  = { environment = var.environment }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

## ─── Invariant Violation Alarm ────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "invariant_violations" {
  alarm_name          = "${var.prefix}-invariant-violations"
  alarm_description   = "Invariant violations detected (any critical invariant broken)"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"

  namespace   = "AEOS"
  metric_name = "invariant_violations_total"
  period      = 60
  statistic   = "Sum"
  dimensions  = { environment = var.environment }

  alarm_actions = local.alarm_actions
  tags          = var.tags
}

## ─── Infrastructure Alarms ────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  count = var.rds_instance_id != "" ? 1 : 0

  alarm_name          = "${var.prefix}-rds-cpu"
  alarm_description   = "RDS CPU > 80%"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 80
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/RDS"
  metric_name = "CPUUtilization"
  period      = 60
  statistic   = "Average"
  dimensions  = { DBInstanceIdentifier = var.rds_instance_id }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

resource "aws_cloudwatch_metric_alarm" "redis_memory" {
  count = var.redis_replication_group_id != "" ? 1 : 0

  alarm_name          = "${var.prefix}-redis-memory"
  alarm_description   = "Redis memory > 80%"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 80
  treat_missing_data  = "notBreaching"

  namespace   = "AWS/ElastiCache"
  metric_name = "DatabaseMemoryUsagePercentage"
  period      = 60
  statistic   = "Average"
  dimensions  = { ReplicationGroupId = var.redis_replication_group_id }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions
  tags          = var.tags
}

## ─── Dashboard ────────────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "aeos" {
  dashboard_name = "${var.prefix}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "API Request Rate"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [[
            "AEOS", "http_requests_total",
            "environment", var.environment,
            { stat = "Sum", period = 60 }
          ]]
        }
      },
      {
        type   = "metric"
        x      = 12, y = 0, width = 12, height = 6
        properties = {
          title  = "Task Throughput (tasks/min)"
          view   = "timeSeries"
          region = var.aws_region
          metrics = [[
            "AEOS", "task_executions_total",
            "status", "success", "environment", var.environment,
            { stat = "Sum", period = 60 }
          ]]
        }
      },
      {
        type   = "alarm"
        x      = 0, y = 6, width = 24, height = 4
        properties = {
          title = "AEOS Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.api_error_rate.arn,
            aws_cloudwatch_metric_alarm.api_p99_latency.arn,
            aws_cloudwatch_metric_alarm.task_failure_rate.arn,
            aws_cloudwatch_metric_alarm.invariant_violations.arn,
          ]
        }
      }
    ]
  })
}
