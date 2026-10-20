# Worker integration with the GitHub state service

The service implementation, deployment and tests have moved to
[TauCetiTools/services/github-state](https://github.com/TauCetiProject/TauCetiTools/tree/main/services/github-state).

Only the example harness's rollout, shadow comparison, observation and storage-recovery commands remain here.
They depend on the worker's local process manager and read transport.
For a downstream installation, use the [project setup manual](https://github.com/TauCetiProject/TauCetiTools/blob/main/docs/project-setup.md#github-state).
