variable "lambda_zip_path" {
  description = "Path to the Lambda deployment package (zip containing index.py)."
  type        = string
  default     = "cf-lambda.zip"
}

variable "schedule_expression" {
  description = "EventBridge Scheduler expression (e.g., rate(6 hours) or cron(...))."
  type        = string
  default     = "rate(6 hours)"
}

variable "cf_v4_max_entries" {
  description = "Max entries (quota ceiling) for the Cloudflare IPv4 prefix list."
  type        = number
  default     = 40
}

variable "cf_v6_max_entries" {
  description = "Max entries (quota ceiling) for the Cloudflare IPv6 prefix list."
  type        = number
  default     = 15
}

variable "slack_notify" {
  description = "Si es true, la Lambda enviará un resumen a Slack en cada ejecución."
  type        = bool
  default     = false
}

variable "slack_webhook_url" {
  description = "Webhook de Slack para notificaciones (Incoming Webhook)."
  type        = string
  default     = ""
  sensitive   = true
}