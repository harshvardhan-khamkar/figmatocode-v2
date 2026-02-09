from pydantic import BaseModel, HttpUrl


class ConvertRequest(BaseModel):
    figma_url: HttpUrl
    framework: str
