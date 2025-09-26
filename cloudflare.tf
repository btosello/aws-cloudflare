########################################
# Managed Prefix Lists (Cloudflare)
########################################
resource "aws_ec2_managed_prefix_list" "cf_v4" {
  name           = "cloudflare-ips-v4"
  address_family = "IPv4"
  max_entries    = var.cf_v4_max_entries
}

resource "aws_ec2_managed_prefix_list" "cf_v6" {
  name           = "cloudflare-ips-v6"
  address_family = "IPv6"
  max_entries    = var.cf_v6_max_entries
}

########################################
# Security Group (only allow HTTPS from Cloudflare)
########################################
resource "aws_security_group" "web" {
  name        = "allow-https-from-cloudflare"
  description = "HTTPS from Cloudflare edge"
  vpc_id      = data.terraform_remote_state.vpc.outputs.vpc_id

  # IPv4 via prefix list
  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [aws_ec2_managed_prefix_list.cf_v4.id]
  }

  # Optional IPv6 via prefix list
  ingress {
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    prefix_list_ids = [aws_ec2_managed_prefix_list.cf_v6.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

########################################
# Lambda: IAM (trust + permissions)
########################################
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "cf-prefixlist-updater-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# Allow Lambda to manage EC2 managed prefix lists
resource "aws_iam_role_policy" "lambda_ec2" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect = "Allow",
      Action = [
        "ec2:DescribeManagedPrefixLists",
        "ec2:GetManagedPrefixListEntries",
        "ec2:ModifyManagedPrefixList"
      ],
      Resource = "*"
    }]
  })
}

# Basic logging for Lambda
resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

########################################
# Lambda Function
########################################
resource "aws_lambda_function" "updater" {
  function_name    = "cf-prefixlist-updater"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "cf-lambda.handler"
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)

  environment {
    variables = {
      PL_V4_ID    = aws_ec2_managed_prefix_list.cf_v4.id
      PL_V6_ID    = aws_ec2_managed_prefix_list.cf_v6.id
      DESCRIPTION = "Cloudflare IP"
      # ---- Slack flags ----
      SLACK_NOTIFY       = tostring(var.slack_notify)
      SLACK_WEBHOOK_URL  = var.slack_webhook_url
    }
  }
}

# Log group 
resource "aws_cloudwatch_log_group" "lg" {
  name              = "/aws/lambda/${aws_lambda_function.updater.function_name}"
  retention_in_days = 14
}

########################################
# EventBridge Scheduler 
########################################
data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler_invoke" {
  name               = "cf-prefixlist-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

resource "aws_iam_role_policy" "scheduler_invoke_lambda" {
  role = aws_iam_role.scheduler_invoke.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Effect   = "Allow",
      Action   = ["lambda:InvokeFunction"],
      Resource = aws_lambda_function.updater.arn
    }]
  })
}

resource "aws_scheduler_schedule" "cf_refresh" {
  name                = "cf-prefixlist-refresh"
  description         = "Refresh Cloudflare prefix lists"
  schedule_expression = var.schedule_expression

  flexible_time_window { mode = "OFF" }

  target {
    arn      = aws_lambda_function.updater.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }

}

