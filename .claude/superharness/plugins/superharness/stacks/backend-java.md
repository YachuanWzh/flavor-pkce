# Backend stack: Java

This project's backend is **Java** (Spring ecosystem). Apply these conventions when working here.

## Layout
- `src/main/java/...` application code; `src/test/java/...` mirrors it.
- Layered: `controller` (thin) -> `service` (business logic) -> `repository` (data).

## Testing (TDD — write the failing test first)
- Test framework: **JUnit 5** + **AssertJ**; mock with **Mockito**.
- Run all: `mvn test` (or `./gradlew test`). Single: `mvn -Dtest=FooServiceTest test`.
- Web layer: `@WebMvcTest` + `MockMvc`. Assert on behavior and HTTP contracts.

## Standards
- Constructor injection (no field `@Autowired`). Keep controllers thin.
- Follow standard Java style; format with the project's plugin (Spotless/google-java-format).
- Run the full `mvn test` (or gradle) suite before claiming done.
