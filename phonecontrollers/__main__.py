import sys

# "--headless" runs only the server, like server.py (handy inside the Flatpak).
if "--headless" in sys.argv:
    sys.argv.remove("--headless")
    from .cli import main

    main()
else:
    from .gui import main

    sys.exit(main())
