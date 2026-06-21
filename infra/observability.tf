locals {
  agent_names = ["orchestrator", "ac-verifier", "security-auditor", "perf-analyzer", "style-enforcer"]
}

resource "aws_cloudwatch_log_group" "agents" {
  for_each          = toset(local.agent_names)
  name              = "/gatecheck/${each.key}"
  retention_in_days = 30
}

resource "aws_xray_group" "gatecheck" {
  group_name        = "${var.project}-agents"
  filter_expression = "annotation.project = \"${var.project}\""
}

resource "aws_cloudwatch_log_metric_filter" "agent_errors" {
  for_each       = toset(local.agent_names)
  name           = "${var.project}-${each.key}-errors"
  log_group_name = aws_cloudwatch_log_group.agents[each.key].name
  pattern        = "ERROR"

  metric_transformation {
    name          = "${replace(each.key, "-", "_")}_errors"
    namespace     = "GateCheck/Agents"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "agent_invocations" {
  for_each       = toset(local.agent_names)
  name           = "${var.project}-${each.key}-invocations"
  log_group_name = aws_cloudwatch_log_group.agents[each.key].name
  pattern        = "handler invoked"

  metric_transformation {
    name          = "${replace(each.key, "-", "_")}_invocations"
    namespace     = "GateCheck/Agents"
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "orchestrator_errors" {
  alarm_name          = "${var.project}-orchestrator-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "orchestrator_errors"
  namespace           = "GateCheck/Agents"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Orchestrator agent logged errors in the last 5 minutes"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_metric_alarm" "security_auditor_errors" {
  alarm_name          = "${var.project}-security-auditor-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "security_auditor_errors"
  namespace           = "GateCheck/Agents"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Security auditor agent logged errors in the last 5 minutes"
  treat_missing_data  = "notBreaching"
}

resource "aws_cloudwatch_dashboard" "gatecheck" {
  dashboard_name = "${var.project}-agents"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 1
        properties = {
          markdown = "# GateCheck — AgentCore Observability"
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 1
        width  = 12
        height = 6
        properties = {
          title  = "Agent Invocations"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            for name in local.agent_names :
            ["GateCheck/Agents", "${replace(name, "-", "_")}_invocations", { label = name }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 1
        width  = 12
        height = 6
        properties = {
          title  = "Agent Errors"
          region = var.aws_region
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            for name in local.agent_names :
            ["GateCheck/Agents", "${replace(name, "-", "_")}_errors", { label = name, color = "#d62728" }]
          ]
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 7
        width  = 24
        height = 6
        properties = {
          title   = "Orchestrator Recent Logs"
          region  = var.aws_region
          view    = "table"
          query   = "SOURCE '/gatecheck/orchestrator' | fields @timestamp, @message | sort @timestamp desc | limit 50"
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 13
        width  = 24
        height = 6
        properties = {
          title   = "All Agent Errors (last 1h)"
          region  = var.aws_region
          view    = "table"
          query   = join("\n", concat(
            ["SOURCE '/gatecheck/orchestrator', '/gatecheck/ac-verifier', '/gatecheck/security-auditor', '/gatecheck/perf-analyzer', '/gatecheck/style-enforcer'"],
            ["| fields @timestamp, @logStream, @message"],
            ["| filter @message like /ERROR/"],
            ["| sort @timestamp desc"],
            ["| limit 100"]
          ))
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 19
        width  = 24
        height = 3
        properties = {
          title = "Agent Alarms"
          alarms = [
            aws_cloudwatch_metric_alarm.orchestrator_errors.arn,
            aws_cloudwatch_metric_alarm.security_auditor_errors.arn,
          ]
        }
      }
    ]
  })
}
