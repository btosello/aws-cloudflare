# aws-cloudflare
AWS and Cloudflare integration

# AWS Security Groups and Cloudflare IP Synchronization

## Overview
This solution ensures that your AWS Security Groups remain synchronized with Cloudflare IP ranges automatically, avoiding manual updates.

## How It Works
1. **Cloudflare CIDRs in Prefix Lists**  
   - Cloudflare’s official IP ranges are stored in **EC2 Managed Prefix Lists**.  
   - One list is used for **IPv4**, and another for **IPv6**.  

2. **Security Groups Reference Prefix Lists**  
   - Instead of pointing directly to individual CIDRs, **Security Groups** reference the **Prefix Lists**.  

3. **Automatic Updates with Lambda**  
   - A **Lambda function** retrieves the latest Cloudflare IP ranges.  
   - It calculates the **delta** (new, removed, or updated ranges).  
   - The Lambda then updates the corresponding **Prefix Lists**.  

4. **Scheduled Execution with EventBridge**  
   - An **EventBridge Scheduler** triggers the Lambda function periodically.  
   - For example, every **6 hours** to ensure IP ranges are always up to date.  

## Benefits
- No manual intervention required  
- Security Groups always reference the latest Cloudflare IPs  
- Supports both **IPv4** and **IPv6** automatically  
- Reliable and scalable using native AWS services  