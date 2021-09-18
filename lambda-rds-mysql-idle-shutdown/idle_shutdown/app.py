import pymysql.cursors
import logging, sys
import math
import json
import boto3


### FOR MANAGING VPC ENDPOINTS
def get_tag(tags, search_tag):
    for tag in tags:
        if tag["Key"].upper() == str(search_tag).upper():
            if str(tag["Value"]).upper() == "TRUE":
                return True
    return False


def shutdown_endpoints():
    client = boto3.client("ec2")

    # could use Filter but its case sensitive sadface
    response = client.describe_vpc_endpoints()

    count_endpoints_total = 0
    count_endpoints_deleted = 0
    count_endpoints_retained = 0

    for endpoint in response["VpcEndpoints"]:
        count_endpoints_total += 1
        # if vpcendpoints_idle_exempt tag is present and set to true, then this won't run
        # but if its not present, or present and set to false, then it will run
        if get_tag(endpoint["Tags"], "VPCENDPOINTS_IDLE_EXEMPT") == False:
            try:
                client.delete_vpc_endpoints(VpcEndpointIds=[endpoint["VpcEndpointId"]])
                count_endpoints_deleted += 1
            except Exception as e:
                print(
                    f"{endpoint['VpcEndpointId']} in {endpoint['VpcId']}: Failed to delete - {str(e)}"
                )
                count_endpoints_retained += 1

        else:
            count_endpoints_retained += 1

    print(
        f"Finished endpoint check. {count_endpoints_deleted} deleted, {count_endpoints_retained} retained, {count_endpoints_total} endpoints total."
    )


###


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
            elif str(tag["Value"]).upper() == "FALSE":
                return False
            else:
                logging.warning(
                    f'{instance["Endpoint"]["Address"]}: Found tag RDS_IDLE_EXEMPT but value was not TRUE or FALSE, so defaulting to NOT idle exempt.  Found value was {tag["Value"]}'
                )
                return False
    # nothing found so assume not exempt
    logging.warning(
        f'{instance["Endpoint"]["Address"]}: Unable to find tag RDS_IDLE_EXEMPT, so defaulting to NOT idle exempt'
    )
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
                f'{instance["Endpoint"]["Address"]}: Processed last command less than 1 hour ago, at {result["db_last_command_time"]}'
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
                    f'{instance["Endpoint"]["Address"]}: Server has been online less than an hour.  Uptime is {uptimeResult["hours"]} hours {uptimeResult["minutes"]} minutes'
                )

                # hasn't executed stuff since it came online, but hasn't been online long enough to really call it idle
                return False
            else:
                # no commands in an hour and has been online for at least an hour, so its idle
                logging.warning(
                    f'{instance["Endpoint"]["Address"]}: Deemed idle.  Server has been up for {uptimeResult["hours"]} hours {uptimeResult["minutes"]} minutes, last command executed {result["db_last_command_time"]}'
                )

                return True

    logging.error(f'{instance["Endpoint"]["Address"]}: Database did not return result!')
    return False


def lambda_handler(event, context):
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    ssmClient = boto3.client("ssm")
    rds = boto3.client("rds", region_name="us-west-2")
    # rds = boto3.client("rds-data")

    rdsInstances = []

    # get rds instances
    try:
        paginator = rds.get_paginator("describe_db_instances").paginate()
        for page in paginator:
            for dbinstance in page["DBInstances"]:
                if isIdleExempt(rds=rds, instance=dbinstance):
                    logging.warning(
                        f'{dbinstance["Endpoint"]["Address"]}: Instance is exempt from idle shutdown'
                    )
                else:
                    logging.warning(
                        f'{dbinstance["Endpoint"]["Address"]}: Instance is NOT exempt from idle shutdown'
                    )

                    rdsInstances.append(dbinstance)

    except Exception as e:
        logging.error("Failed to enumerate RDS instances. Traceback follows.")
        logging.error(str(e))
        raise

    # just to make sure this variable isn't unintentionally accessed later
    dbinstance = None

    # now try and connect to the RDS instances
    # if the instance is online, see if its been idle
    # if it has, turn it off
    for instance in rdsInstances:
        logging.warning(f'{instance["Endpoint"]["Address"]}: Checking instance')

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
                            logging.warning(
                                f'{instance["Endpoint"]["Address"]}: Instance is idle.  Successfully issued shutdown command.'
                            )

                        except Exception as e:
                            logging.error(
                                f'{instance["Endpoint"]["Address"]}: Failed to stop RDS instance. Traceback follows.'
                            )

                            logging.error(str(e))
                            raise

                        # also kill off VPC endpoints - I'm assuming no RDS means no need for VPC endpoints
                        shutdown_endpoints()
                    else:
                        logging.warning(
                            f'{instance["Endpoint"]["Address"]}: Instance not idle.  Skipping.'
                        )
        else:
            # skipping instance, its not powered on
            logging.warning(
                f'{instance["Endpoint"]["Address"]}: Instance is not powered on.  Ignoring.'
            )
            shutdown_endpoints()

    return {"statusCode": 200, "body": json.dumps("Success")}


if __name__ == "__main__":
    lambda_handler("", "")
