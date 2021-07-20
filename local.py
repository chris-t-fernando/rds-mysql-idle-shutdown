import pymysql.cursors
import boto3
import logging, sys
import json
from datetime import datetime
from datetime import timedelta

logging.basicConfig(stream=sys.stderr, level=logging.WARNING)


# WIGGLE_ROOM is the amount that Com_Select can increase by without me considering the server to be 'busy'
# seems like it increases by 14 just by running this check
WIGGLE_ROOM = 60

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


def updateIdle(ssmClient, lastSelect, instance, idleValue):
    lastSelect["selectCount"] = int(idleValue) + WIGGLE_ROOM
    lastSelect["timestamp"] = datetime.now()
    ssmClient.put_parameter(
        Name="/platform/rds-idle-shutdown-" + instance["Endpoint"]["Address"],
        Value=json.dumps(lastSelect, default=str),
        Overwrite=True,
        Type="String",
    )
    logging.warning(
        "%s: Wrote updated check to path %s",
        instance["Endpoint"]["Address"],
        "/platform/rds-idle-shutdown-" + instance["Endpoint"]["Address"],
    )


def isIdle(ssmClient, lastSelect, instance, cursor):
    sqlSelect = 'show global status like "Com_select"'
    cursor.execute(sqlSelect)
    result = cursor.fetchone()

    if "Value" in result.keys():
        # have queries been processsed since last check?
        if int(result["Value"]) < (int(lastSelect["selectCount"]) + WIGGLE_ROOM):
            # if uptime is less than last timestamp, its been rebooted
            # therefore return False but also updateIdle for next run
            sqlUptime = "select TIME_FORMAT(SEC_TO_TIME(VARIABLE_VALUE ),'%H') as hours, TIME_FORMAT(SEC_TO_TIME(VARIABLE_VALUE ),'%i') as minutes from performance_schema.global_status      where VARIABLE_NAME='Uptime'"
            cursor.execute(sqlUptime)
            uptimeResult = cursor.fetchone()
            startupTime = datetime.now() - timedelta(
                hours=int(uptimeResult["hours"]), minutes=int(uptimeResult["minutes"])
            )

            if startupTime > lastSelect["timestamp"]:
                # started up more recently than last select
                # eg. lastSelect is 19/07 8pm
                # startupTime is 20/07 11:30am
                # therefore RDS was recently started
                logging.warning(
                    "%s: Recently started up, assuming not idle.  Server has been up for %s hours %s minutes",
                    instance["Endpoint"]["Address"],
                    uptimeResult["hours"],
                    uptimeResult["minutes"],
                )
                updateIdle(
                    ssmClient=ssmClient,
                    lastSelect=lastSelect,
                    instance=instance,
                    idleValue=result["Value"],
                )
                return False
            else:
                # versus lastSelect 20/07 at 10am and startupTime was 16/07 8pm - lastSelect is more recent, so no restart/start has occurred
                logging.warning(
                    "%s: Did not start up recently.  Server has been up for %s hours %s minutes",
                    instance["Endpoint"]["Address"],
                    uptimeResult["hours"],
                    uptimeResult["minutes"],
                )

            # no queries since last run
            if (datetime.now() - timedelta(hours=1)) > lastSelect["timestamp"]:
                logging.warning(
                    "%s: No queries for more than 1 hour",
                    instance["Endpoint"]["Address"],
                )
                updateIdle(
                    ssmClient=ssmClient,
                    lastSelect=lastSelect,
                    instance=instance,
                    idleValue=result["Value"],
                )
                return True
            else:
                logging.warning(
                    "%s: No queries but last check was less than 1 hour ago, not enough time has elapsed to assume idle",
                    instance["Endpoint"]["Address"],
                )
                updateIdle(
                    ssmClient=ssmClient,
                    lastSelect=lastSelect,
                    instance=instance,
                    idleValue=result["Value"],
                )
                return False

        else:
            # queries have happened, so its not idle
            logging.warning(
                "%s: Instance has processed more queries.  DB: %s vs SSM parameter: %s",
                instance["Endpoint"]["Address"],
                result["Value"],
                lastSelect["selectCount"],
            )
            updateIdle(
                ssmClient=ssmClient,
                lastSelect=lastSelect,
                instance=instance,
                idleValue=result["Value"],
            )
            return False

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

                    # show status where variable_name = 'threads_connected';
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
            mydb = pymysql.connect(
                host=instance["Endpoint"]["Address"],
                user=getSSMParameter(
                    ssmClient=ssmClient,
                    path="/platform/rds-idle-shutdown-username",
                ),
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
                    # look up last select count
                    # returns a dict
                    # lastSelect['selectCount'] = nnn
                    # lastSelect['timestamp'] = yyyy-mm-dd hh-mm
                    lastSelect = {}
                    try:
                        lastSelect = json.loads(
                            getSSMParameter(
                                ssmClient=ssmClient,
                                path="/platform/rds-idle-shutdown-"
                                + instance["Endpoint"]["Address"],
                            )
                        )

                        # need to format the date as an object
                        lastSelect["timestamp"] = datetime.strptime(
                            lastSelect["timestamp"], "%Y-%m-%d %H:%M:%S.%f"
                        )

                        logging.warning(
                            "%s: Successfully pulled last idle result from SSM",
                            instance["Endpoint"]["Address"],
                        )

                    except:
                        # couldn't find it, so assume its a new RDS instance
                        lastSelect["selectCount"] = 0
                        lastSelect["timestamp"] = datetime.now()
                        logging.warning(
                            "%s:Unable to pull last idle result from SSM.  Searched key was %s.  New RDS instance?",
                            instance["Endpoint"]["Address"],
                            "/platform/rds-idle-shutdown-"
                            + instance["Endpoint"]["Address"],
                        )

                    if isIdle(
                        cursor=cursor,
                        lastSelect=lastSelect,
                        instance=instance,
                        ssmClient=ssmClient,
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


lambda_handler("blah", "blah")
