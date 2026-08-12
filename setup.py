from setuptools import setup, find_packages

setup(
    name="multimodal_agentic_deep_research_assistant",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "pydantic>=2.0",
        "fastapi>=0.100.0",
        "uvicorn>=0.25.0",
    ],
    extras_require={
        "test": [
            "pytest>=7.0",
            "pytest-asyncio>=0.23.0",
        ],
    },
    python_requires=">=3.11,<3.13",
)
