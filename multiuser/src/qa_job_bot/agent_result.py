from copy import deepcopy


class AgentResponse(dict):
    def __init__(self, response, metadata):
        super().__init__(deepcopy(response))
        self._metadata = deepcopy(metadata)

    @property
    def metadata(self):
        return deepcopy(self._metadata)


class AgentText(str):
    def __new__(cls, value, metadata):
        result = super().__new__(cls, value)
        result._metadata = deepcopy(metadata)
        return result

    @property
    def metadata(self):
        return deepcopy(self._metadata)


def response_metadata(response):
    return response.metadata if isinstance(response, (AgentResponse, AgentText)) else {}
