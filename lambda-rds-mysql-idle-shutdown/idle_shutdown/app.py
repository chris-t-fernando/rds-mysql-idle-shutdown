import pymysql.cursors
import boto3
import logging, sys
import math

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

# handler for pulling config from SSM
def getSSMParameter(ssmClient, path, encryptionOption=False):
    return (
        ssmClient.get_parameter(Name=path, WithDecryption=encryptionOption)
        .get("Parameter")
        .get("Value")
    )


def isIdleExempt(rds, instance):
    tags = rds.list_tags_for_resource(ResourceName=instance["DBInstanceArn"])
    for tag in tags["TagList"]:
        if str(tag["Key"]).upper() == "RDS_IDLE_EXEMPT":
            if str(tag["Value"]).upper() == "TRUE":
                return True
            else:
                return False
    # nothing found so assume not exempt
    return False


def isIdle(instance, cursor, user):
    sqlSelect = "select event_time as db_last_command_time, user_host from mysql.general_log where user_host not like %s and user_host not like %s order by event_time desc limit 1"
    cursor.execute(sqlSelect, ("%rdsadmin%", "%" + user + "%"))
    result = cursor.fetchone()

    if "db_last_command_time" in result.keys():
        # have queries been processsed since last check?
        sqlSelectNow = "select now()"
        cursor.execute(sqlSelectNow)
        resultNow = cursor.fetchone()
        elapsed = resultNow["now()"] - result["db_last_command_time"]
        if math.floor(elapsed.total_seconds() / (60 * 60)) < 1:
            # its been less than 1 hour since a command was executed
            logging.warning(
                "%s: Processed last command less than 1 hour ago, at %s",
                instance["Endpoint"]["Address"],
                result["db_last_command_time"],
            )

            # returns False because the instance is not idle
            return False
        else:
            # its been more than an hour since a command was executed
            # but how long has the server been up? if less than an hour, give it a stay of execution
            sqlUptime = "select TIME_FORMAT(SEC_TO_TIME(VARIABLE_VALUE ),'%H') as hours, TIME_FORMAT(SEC_TO_TIME(VARIABLE_VALUE ),'%i') as minutes from performance_schema.global_status      where VARIABLE_NAME='Uptime'"
            cursor.execute(sqlUptime)
            uptimeResult = cursor.fetchone()

            if int(uptimeResult["hours"]) < 1:
                # started up less than an hour ago
                logging.warning(
                    "%s: Server has been online less than an hour.  Uptime is %s hours %s minutes",
                    instance["Endpoint"]["Address"],
                    uptimeResult["hours"],
                    uptimeResult["minutes"],
                )

                # hasn't executed stuff since it came online, but hasn't been online long enough to really call it idle
                return False
            else:
                # no commands in an hour and has been online for at least an hour, so its idle
                logging.warning(
                    "%s: Deemed idle.  Server has been up for %s hours %s minutes, last command executed %s",
                    instance["Endpoint"]["Address"],
                    uptimeResult["hours"],
                    uptimeResult["minutes"],
                    result["db_last_command_time"],
                )

                return True
    logging.error(
        "%s: Database did not return result!", instance["Endpoint"]["Address"]
    )
    return False


def lambda_handler(event, context):
    ssmClient = boto3.client("ssm")
    rds = boto3.client("rds", region_name="us-west-2")

    rdsInstances = []

    # get rds instances
    try:
        paginator = rds.get_paginator("describe_db_instances").paginate()
        for page in paginator:
            for dbinstance in page["DBInstances"]:
                if isIdleExempt(rds=rds, instance=dbinstance):
                    logging.info(
                        "%s: Instance is exempt from idle shutdown",
                        dbinstance["Endpoint"]["Address"],
                    )
                else:
                    logging.info(
                        "%s: Instance is NOT exempt from idle shutdown",
                        dbinstance["Endpoint"]["Address"],
                    )

                    rdsInstances.append(dbinstance)

    except Exception as e:
        print("Failed to enumerate RDS instances. Traceback follows.")
        print(str(e))
        print("Exiting.")

    # just to make sure this variable isn't unintentionally accessed later
    dbinstance = None

    # now try and connect to the RDS instances
    # if the instance is online, see if its been idle
    # if it has, turn it off
    for instance in rdsInstances:
        logging.debug("%s: Checking instance", instance["Endpoint"]["Address"])

        # check if its online
        if instance["DBInstanceStatus"] == "available":
            # try connect to it
            # hold on to user - its used later
            user = getSSMParameter(
                ssmClient=ssmClient,
                path="/platform/rds-idle-shutdown-username",
            )
            mydb = pymysql.connect(
                host=instance["Endpoint"]["Address"],
                user=user,
                password=getSSMParameter(
                    ssmClient=ssmClient,
                    path="/platform/rds-idle-shutdown-password",
                    encryptionOption=True,
                ),
                database="sys",
                cursorclass=pymysql.cursors.DictCursor,
            )

            with mydb:
                with mydb.cursor() as cursor:
                    if isIdle(
                        cursor=cursor,
                        instance=instance,
                        user=user,
                    ):
                        try:
                            rds.stop_db_instance(
                                DBInstanceIdentifier=instance["DBInstanceIdentifier"]
                            )
                            print(
                                "%s: Instance is idle.  Successfully issued shutdown command.",
                                instance["Endpoint"]["Address"],
                            )
                        except Exception as e:
                            print(
                                "%s: Failed to stop RDS instance. Traceback follows.",
                                instance["Endpoint"]["Address"],
                            )
                            print(str(e))
                    else:
                        logging.warning(
                            "%s: Instance not idle.  Skipping.",
                            instance["Endpoint"]["Address"],
                        )
        else:
            # skipping instance, its not powered on
            logging.warning(
                "%s: Instance is not powered on.  Ignoring.",
                instance["Endpoint"]["Address"],
            )
