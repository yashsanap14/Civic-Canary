from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    SecretValue,
    Stack,
)
from aws_cdk import (
    aws_apigatewayv2 as apigwv2,
)
from aws_cdk import (
    aws_apigatewayv2_integrations as integrations,
)
from aws_cdk import (
    aws_bedrockagentcore as bedrockagentcore,
)
from aws_cdk import (
    aws_dynamodb as dynamodb,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from aws_cdk import (
    aws_scheduler as scheduler,
)
from aws_cdk import (
    aws_secretsmanager as secretsmanager,
)
from aws_cdk import (
    aws_ssm as ssm,
)
from constructs import Construct

ROOT = Path(__file__).resolve().parents[1]


class CivicCanaryStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        review_token_digest: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        evidence = s3.Bucket.from_bucket_name(self, "Evidence", "civic-canary")
        sites = dynamodb.Table.from_table_name(self, "Sites", "CivicCanarySites")
        findings = dynamodb.Table.from_table_name(self, "Findings", "CivicCanaryFindings")
        reviews = dynamodb.Table.from_table_name(self, "Reviews", "CivicCanaryReviews")
        review_secret = secretsmanager.Secret(
            self,
            "ReviewToken",
            description="Civic Canary demo reviewer token",
            secret_string_value=SecretValue.unsafe_plain_text(
                f"sha256:{review_token_digest}"
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        browser_recording_role = iam.Role(
            self,
            "BrowserRecordingRole",
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {
                        "aws:SourceArn": (
                            f"arn:{self.partition}:bedrock-agentcore:"
                            f"{self.region}:{self.account}:*"
                        )
                    },
                },
            ),
        )
        evidence.grant_read_write(browser_recording_role)
        browser_recording_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogStreams",
                ],
                resources=[
                    f"arn:{self.partition}:logs:{self.region}:{self.account}:"
                    "log-group:/aws/bedrock-agentcore/browser/*"
                ],
            )
        )
        managed_browser = bedrockagentcore.CfnBrowserCustom(
            self,
            "ManagedBrowser",
            name="CivicCanaryBrowser",
            description="Read-only Civic Canary portal capture with session replay",
            network_configuration=(
                bedrockagentcore.CfnBrowserCustom.BrowserNetworkConfigurationProperty(
                    network_mode="PUBLIC"
                )
            ),
            execution_role_arn=browser_recording_role.role_arn,
            recording_config=bedrockagentcore.CfnBrowserCustom.RecordingConfigProperty(
                enabled=True,
                s3_location=bedrockagentcore.CfnBrowserCustom.S3LocationProperty(
                    bucket=evidence.bucket_name,
                    prefix="browser-recordings/",
                ),
            ),
        )
        runtime_parameter = ssm.StringParameter(
            self,
            "AgentRuntimeArn",
            parameter_name="/civic-canary/agent-runtime-arn",
            string_value="UNCONFIGURED",
            description="AgentCore Runtime ARN, written after agent deployment",
        )
        storage_parameter = ssm.StringParameter(
            self,
            "StorageConfig",
            parameter_name="/civic-canary/storage-config",
            string_value=self.to_json_string(
                {
                    "sites_table": sites.table_name,
                    "reviews_table": reviews.table_name,
                    "findings_table": findings.table_name,
                    "evidence_bucket": evidence.bucket_name,
                    "browser_id": managed_browser.attr_browser_id,
                }
            ),
            description="Resource names consumed by the Civic Canary AgentCore runtime",
        )

        lambda_asset = lambda_.Code.from_asset(str(ROOT / "build" / "lambda"))
        common_environment = {
            "AWS_REGION_NAME": self.region,
            "CIVIC_CANARY_MODE": "aws",
            "CIVIC_CANARY_SITES_TABLE": sites.table_name,
            "CIVIC_CANARY_REVIEWS_TABLE": reviews.table_name,
            "CIVIC_CANARY_FINDINGS_TABLE": findings.table_name,
            "CIVIC_CANARY_S3_BUCKET": evidence.bucket_name,
            "REVIEW_TOKEN_SECRET_ARN": review_secret.secret_arn,
            "AGENT_RUNTIME_SSM_PARAMETER": runtime_parameter.parameter_name,
        }
        api_function = lambda_.Function(
            self,
            "ControlApi",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="services.control_api.lambda_handler.handler",
            code=lambda_asset,
            memory_size=768,
            timeout=Duration.seconds(30),
            environment=common_environment,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                "ControlApiLogs",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )
        schedule_function = lambda_.Function(
            self,
            "ScheduledRunner",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="services.scheduled_handler.handler",
            code=lambda_asset,
            memory_size=1024,
            timeout=Duration.minutes(15),
            environment=common_environment,
            tracing=lambda_.Tracing.ACTIVE,
            log_group=logs.LogGroup(
                self,
                "ScheduledRunnerLogs",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            ),
        )

        for function in (api_function, schedule_function):
            sites.grant_read_write_data(function)
            reviews.grant_read_write_data(function)
            findings.grant_read_write_data(function)
            evidence.grant_read_write(function)
            review_secret.grant_read(function)
            runtime_parameter.grant_read(function)
            storage_parameter.grant_read(function)
            function.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["bedrock-agentcore:InvokeAgentRuntime"],
                    resources=["*"],
                )
            )

        api_function.add_environment(
            "SCAN_WORKER_FUNCTION_NAME", schedule_function.function_name
        )
        schedule_function.grant_invoke(api_function)

        api = apigwv2.HttpApi(
            self,
            "HttpApi",
            default_integration=integrations.HttpLambdaIntegration(
                "ControlIntegration", api_function
            ),
        )
        default_stage = api.default_stage.node.default_child
        default_stage.default_route_settings = apigwv2.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=20,
            throttling_rate_limit=10,
        )

        api_function.add_environment("CORS_ORIGINS", api.api_endpoint)

        scheduler_role = iam.Role(
            self,
            "SchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        schedule_function.grant_invoke(scheduler_role)
        scheduler.CfnSchedule(
            self,
            "DailySchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(mode="OFF"),
            schedule_expression="cron(0 9 * * ? *)",
            schedule_expression_timezone="America/New_York",
            state="ENABLED",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=schedule_function.function_arn,
                role_arn=scheduler_role.role_arn,
                input='{"target_id":"benefits-demo"}',
            ),
        )

        CfnOutput(self, "ApiUrl", value=api.api_endpoint)
        CfnOutput(
            self,
            "FrontendUrl",
            value=api.api_endpoint,
        )
        CfnOutput(self, "EvidenceBucket", value=evidence.bucket_name)
        CfnOutput(self, "SitesTable", value=sites.table_name)
        CfnOutput(self, "ReviewsTable", value=reviews.table_name)
        CfnOutput(self, "FindingsTable", value=findings.table_name)
        CfnOutput(self, "ReviewTokenSecretArn", value=review_secret.secret_arn)
        CfnOutput(self, "AgentRuntimeParameter", value=runtime_parameter.parameter_name)
        CfnOutput(self, "StorageConfigParameter", value=storage_parameter.parameter_name)
        CfnOutput(self, "ManagedBrowserId", value=managed_browser.attr_browser_id)

