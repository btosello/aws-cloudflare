output "cloudflare_prefix_list_v4_id" {
  value       = aws_ec2_managed_prefix_list.cf_v4.id
  description = "Managed Prefix List ID for Cloudflare IPv4 ranges."
}

output "cloudflare_prefix_list_v6_id" {
  value       = aws_ec2_managed_prefix_list.cf_v6.id
  description = "Managed Prefix List ID for Cloudflare IPv6 ranges."
}

output "security_group_id" {
  value       = aws_security_group.web.id
  description = "Security Group restricted to Cloudflare."
}

output "scheduler_name" {
  value       = aws_scheduler_schedule.cf_refresh.name
  description = "EventBridge Scheduler name that invokes the updater Lambda."
}