import asyncio

from qa_job_bot.agent_result import response_metadata
from qa_job_bot.api_audit import CALL_AUDIT


class MeteredGateway:
    def __init__(self, gateway, limits):
        self.gateway, self.limits = gateway, limits

    async def invoke(self, method, context, *arguments):
        role = {
            "generate": "generator",
            "review": "reviewer",
            "visual_review": "visual_reviewer",
        }.get(method, method)
        identifier = None
        async with asyncio.timeout(240):
            while identifier is None:
                identifier = self.limits.reserve(context, role)
                if identifier is None:
                    await asyncio.sleep(0.25)
        token = CALL_AUDIT.set(lambda event, data: self.limits.progress(identifier, event, data))
        succeeded = False
        try:
            result = await getattr(self.gateway, method)(context, *arguments)
            metadata = response_metadata(result)
            self.limits.progress(identifier, "result", metadata)
            succeeded = True
            return result
        finally:
            CALL_AUDIT.reset(token)
            self.limits.finish(identifier, succeeded)

    async def generate(self, context):
        return await self.invoke("generate", context)

    async def review(self, context, draft):
        return await self.invoke("review", context, draft)

    async def visual_review(self, context, draft, rendered_reference):
        return await self.invoke("visual_review", context, draft, rendered_reference)

    async def chat(self, context, messages):
        return await self.invoke("chat", context, messages)
