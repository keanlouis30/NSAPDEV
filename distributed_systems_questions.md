# Distributed Systems: "Mini-Splunk" Syslog Analytics Server
## Professor's Evaluation Questions and Answers

As a professor evaluating the implementation of the "Mini-Splunk" Server Application project, I would focus on the student's understanding of concurrency, synchronization primitives, state management, and client-server communication protocols. Below are 10 specific questions I would ask during an evaluation, along with the expected concise answers.

### 1. Concurrency Bottlenecks
**Question:** Why is it critical that the server uses multithreading or asynchronous I/O for the `INGEST` operation, and what would happen to the system if requests were processed synchronously?
**Answer:** Synchronous processing would block the server's main loop during a heavy I/O operation (like a multi-megabyte log upload). This would cause starvation, preventing any other clients from connecting, uploading, or querying the server until the current upload finishes. Multithreading allows the server to keep listening and processing other clients operating concurrently.

### 2. Synchronization Mechanisms
**Question:** When executing a `PURGE` command, what specific type of lock must be employed on the shared state, and why is this necessary?
**Answer:** An exclusive lock (or the write portion of a reader-writer lock) must be acquired. This ensures that no other threads are reading (querying) or writing (ingesting) while the data is being cleared, preventing race conditions, dirty reads, or `IndexError` exceptions caused by modifying the data structure while another thread is iterating over it.

### 3. Critical Sections
**Question:** During the `INGEST` stage, the server parses logs using regular expressions. Should this parsing occur inside or outside the critical section (the locked portion of code), and why?
**Answer:** Parsing must occur *outside* the critical section. Regular expression parsing is CPU-bound and time-consuming. Holding the lock while parsing would needlessly block other threads. The lock should only be acquired for the brief moment the parsed data is being inserted into the shared in-memory structure.

### 4. Lock Granularity 
**Question:** If you are using a single, global Mutex (`threading.Lock`) to protect the central log storage, how does this affect the performance of concurrent `SEARCH_*` operations, and what is a superior alternative?
**Answer:** A global Mutex forces all reads (searches) to happen sequentially, creating a bottleneck. A superior alternative is a Reader-Writer lock (`RWLock`), which allows multiple threads to search the data concurrently (shared read lock) but enforces exclusive access during `INGEST` or `PURGE` operations (exclusive write lock).

### 5. Inter-Process Communication (IPC) vs Threads
**Question:** The specifications allow either multithreading or multiprocessing. In Python, due to the Global Interpreter Lock (GIL), what are the trade-offs of using `multiprocessing` instead of `threading` for this specific architecture?
**Answer:** `threading` shares memory natively, making it trivial to maintain a single central log index, but Python's GIL limits CPU-bound parsing performance. `multiprocessing` bypasses the GIL to utilize multiple CPU cores for parsing, but it requires complex IPC (like a `Manager` object or shared memory segments) to synchronize the central log index across isolated process memory spaces.

### 6. Protocol Design and Command Parsing
**Question:** How do you design the application-layer protocol so the server thread instantly knows whether it should stream a large incoming file (`INGEST`) or process a brief string search (`QUERY ... SEARCH_...`)?
**Answer:** The server must read a predetermined chunk size (e.g., the first 1024 bytes or up to a newline separator) upon connection. This header dictates the mode. If the header starts with `INGEST`, the server enters a loop to write the remaining stream buffer to a parsing queue. If it starts with `QUERY`, it parses the command parameters and routes to the search logic.

### 7. Managing Network Stream Boundaries
**Question:** TCP is a streaming protocol, not a message-based one. During `INGEST`, how does the server guarantee it does not parse half of a log line if a TCP packet fragments exactly in the middle of a log entry?
**Answer:** The server must implement a stream buffer. It appends incoming bytes to the buffer and only extracts strings up to the last `\n` (newline character). Any leftover characters that do not complete a line are kept in the buffer until the next TCP chunk arrives to complete it.

### 8. Race Conditions in Data Aggregation
**Question:** Describe a specific race condition that could occur if no locks were used while client A calls `COUNT_KEYWORD "ERROR"` and client B calls `INGEST`.
**Answer:** Client A's thread iterates over the log list to count "ERROR". Simultaneously, Client B's thread appends new log lines to the list or triggers a dynamic array resize in memory. Client A's iterator might skip elements or throw an out-of-bounds runtime exception due to the underlying array resizing mid-iteration.

### 9. State Persistence vs. In-Memory
**Question:** If the server is using an strictly in-memory data structure for speed, what happens to the ingested data if the server process crashes, and how could you mitigate this without entirely sacrificing the speed of in-memory searches?
**Answer:** An strictly in-memory structure will lose all data upon a crash. To mitigate this, the server can use a Write-Ahead Log (WAL). Incoming `INGEST` streams are quickly appended to a persistent disk file first, and then populated into the in-memory data structure for ultra-fast `SEARCH` querying.

### 10. Distributed Scaling
**Question:** Currently, this is a single centralized server. If the index became too large and we needed to scale horizontally to *three* log servers behind a load balancer, why would your current thread-safe shared state mechanism fail, and what must replace it?
**Answer:** A local `threading.Lock` only protects memory within a single machine's process. With three servers, they have separate physical memory spaces, meaning an `INGEST` on Server 1 won't be seen by a `SEARCH` on Server 2. The local thread-safe structure must be replaced by an external, distributed, concurrent data store (like Redis, Elasticsearch, or a distributed database) and use distributed locking.
