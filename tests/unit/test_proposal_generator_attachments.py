import pytest

from src.action.proposal_generator import ProposalGenerator
from src.parsers.base_parser import ProjectItem


def _project_with_file() -> ProjectItem:
    return ProjectItem(
        id="p1",
        title="FreePBX transfer work",
        description="Need FreePBX changes.",
        budget=2000,
        currency="RUB",
        skills=["asterisk"],
        url="https://kwork.ru/projects/p1",
        platform="kwork",
        created_at="",
        platform_data={
            "files": [
                {
                    "fname": "brief.pdf",
                    "url": "https://example.com/brief.pdf",
                    "size": 123,
                }
            ]
        },
    )


def test_attachment_files_from_platform_data():
    generator = ProposalGenerator()

    files = generator._attachment_files(_project_with_file())

    assert files[0]["name"] == "brief.pdf"
    assert files[0]["url"] == "https://example.com/brief.pdf"


@pytest.mark.asyncio
async def test_attachment_context_added_to_prompt(monkeypatch):
    generator = ProposalGenerator()

    async def fake_download(_client, _file_info, *, max_bytes, cookies):
        assert max_bytes > 0
        assert isinstance(cookies, dict)
        return "AMI events, attended transfer, CallerID rewrite, webhook delivery"

    monkeypatch.setattr(generator, "_download_attachment_text", fake_download)

    context = await generator._build_attachment_context(_project_with_file())
    prompt = generator._build_user_prompt(
        _project_with_file(),
        attachment_context=context,
    )

    assert "brief.pdf" in prompt
    assert "AMI events" in prompt
    assert "webhook delivery" in prompt
