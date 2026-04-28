"""Built-in tool implementations for ScholarLab's agentic turn loop."""

from researchclaw.pipeline.claw_engine.tools.builtin.file_tools import (
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
)
from researchclaw.pipeline.claw_engine.tools.builtin.search_tools import (
    GlobSearchTool,
    GrepSearchTool,
)
from researchclaw.pipeline.claw_engine.tools.builtin.bash_tool import BashTool
from researchclaw.pipeline.claw_engine.tools.builtin.research_tools import (
    LatexCompileTool,
    BibSearchTool,
    DataAnalysisTool,
    WebSearchTool,
)

ALL_BUILTIN_TOOLS = [
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobSearchTool(),
    GrepSearchTool(),
    BashTool(),
    LatexCompileTool(),
    BibSearchTool(),
    DataAnalysisTool(),
    WebSearchTool(),
]

__all__ = [
    "ALL_BUILTIN_TOOLS",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "GlobSearchTool",
    "GrepSearchTool",
    "BashTool",
    "LatexCompileTool",
    "BibSearchTool",
    "DataAnalysisTool",
    "WebSearchTool",
]
