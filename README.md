# rds-mysql-idle-shutdown
Shut down MySQL if its idle (no queries in last hour and no current sessions)

I found I kept on leaving my RDS instance running even when it wasn't in use.  So I wrote a Lambda function which:
1. Queries RDS for a list of RDS instances
1. Ignores instances which are shut down (obviously..)
1. Examines the tags on each RDS instance.  If it finds the tag RDS_IDLE_EXEMPT (case insensitive) with a value of True (also case insensitive), it ignores it.  Otherwise...
1. It logs in to the MySQL instance and queries uptime and time of last command
1. If uptime is more than 1 hour AND last command is more than 1 hour ago, the instance is shut down and I save a couple bucks

The concept would conceivably scale out quite well.  Over multiple instances (plus replicas) and larger instance sizes, something like this could save a bunch of money in non-production environments

Todo (the first two are probably the only ones that I care enough to go back and fix though:
1. Use IAM roles to login to mysql instead of pulling credentials from SSM
1. If the lambda function cannot log in to the mysql instance, shut down the instance rather than just ignoring it
1. Minor improvements to logging required - reduce verbosity/change from warn() to info() and debug().  Also some messages aren't processing %s properly, not sure what's up
1. Automate full config - RDS parameter settings, Lambda permissions
