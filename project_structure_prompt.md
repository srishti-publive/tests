The project is already working correctly and all current functionality should remain exactly as it is. Please do not change any business logic, workflows, API behavior, authentication flow, database operations, validations, request/response structures, or existing outputs.

The goal is only to improve code quality and make the project look and feel like production-ready code.

Things to focus on:

* Use clear and meaningful variable, function, class, and file names so that their purpose is immediately understandable.
* Break down large files into smaller, well-organized modules and use imports appropriately.
* Follow the principle of one function = one responsibility. If a function is doing multiple things, split it into smaller focused functions.
* Keep views responsible for handling requests and responses only.
* Keep serializers responsible for validation and serialization only.
* Move business logic into dedicated service/helper layers where appropriate.
* Remove duplicate code and follow DRY principles.
* Extract reusable utilities, constants, validators, and common logic into separate files.
* Add concise docstrings and type hints where they improve readability.
* Improve code organization and folder structure for long-term maintainability.
* Standardize logging, exception handling, and configuration management.
* Replace hardcoded values with constants or configuration variables where appropriate.
* Improve readability of database queries without changing their behavior.
* Follow consistent coding conventions throughout the project.

While refactoring, think like a developer joining this project six months from now. The code should be easy to navigate, easy to understand, and easy to extend without needing to trace through large files or unclear function names.

Most importantly:

* No feature changes.
* No logic changes.
* No API contract changes.
* No database behavior changes.
* No authentication or authorization changes.
* No optimization that alters functionality.

The final result should behave exactly the same as it does today, with the only difference being cleaner structure, better readability, improved maintainability, and production-level code organization.