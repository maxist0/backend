#app.py
"""
Entry point for the API application.

Usage:
    python app.py dev      # Development environment
    python app.py test     # Test environment  
    python app.py stage    # Staging environment
    python app.py prod     # Production environment
"""

import argparse


def main():
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument(
        "env",
        nargs="?",
        default="dev",
        choices=["dev", "test", "stage", "prod"],
        help="Environment to run (default: dev)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host to bind to (default: from settings)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind to (default: from settings)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        default=None,
        help="Enable auto-reload (default: from settings)",
    )
    
    args = parser.parse_args()
    
    # Initialize settings BEFORE importing the app
    from src.common.config import init_settings
    settings = init_settings(args.env)
    
    # Setup logging with file handlers using settings from config
    from src.common.logging import setup_logging
    _log_dir = setup_logging(settings)
    

    # Now import and run the app
    import uvicorn
    
    # Use command-line args if provided, otherwise use settings from config
    host = args.host if args.host is not None else settings.HOST
    port = args.port if args.port is not None else settings.PORT
    reload_enabled = args.reload if args.reload is not None else settings.RELOAD
    
    uvicorn.run(
        "src.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
        log_config=None,  # Disable uvicorn's default logging configuration
        reload_includes=["src/", "app.py"],  # Watch src/ and app.py for changes
    )


if __name__ == "__main__":
    main()