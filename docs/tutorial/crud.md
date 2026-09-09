# Create, update, delete

CRUD documentation moved to:

- **Collections (adopt/move/detach/trash/delete_permanently):** [Drive collections tutorial](collection.md)
- **Sheet rows (session add/commit/delete):** [Sheet tables tutorial — §4](sheets.md#4-create-update-delete)

Remember: `session.delete()` on collection members **trashes** the Drive file. Permanent deletion requires `collection.delete_permanently(file_id)`.

See [Consistency & errors](../consistency/errors.md#delete-semantics).
