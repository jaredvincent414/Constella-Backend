# Implementation Details

## 1. Graph Model Representation (Directed Acyclic Graph / DAG)

Academic trajectories are naturally modeled as a multi-stage directed graph:

**Nodes:** Represent academic artifacts at distinct semester stages:

- Course nodes indexed by timeline: (Stage: Freshman, Course: Intro Bio)
- Milestone & outcome nodes: (Major Change: Pre-Med -> Biochem), (Career Outcome: Health Policy Analyst)

**Edges (Directed & Weighted):** Represent transitions between stages:

- Edges connect a course in Stage $N$ to a course in Stage $N+1$ taken by an alumnus.
- Edge weights represent the aggregate transition frequency/flow across the historical dataset.

## 2. In-Memory Traversal & Path Merging Logic

When a student queries the system or combines saved paths, the backend traverses and synthesizes the graph:

```
[Student Profile: Stage 0..N]
          │
          ▼  (Graph Traversal & Frequency Scoring)
[Adjacency Matrices / DAG Nodes] ──▶ Resolve Prerequisite Conflicts
          │
          ▼
[Merged Consolidated Trajectory (Sankey)]
```

**Directional Subgraph Matching:** The similarity engine calculates the intersection of the student's completed node array against historical alumni subgraphs, weighting pre-pivot course overlap (50%) and transition timing (20%).

**Consensus & Conflict Resolution:** When merging multiple alumni paths, nodes at each semester stage are ranked by in-degree frequency. If two courses occupy the same prerequisite chain, the algorithm prioritizes the higher-frequency node to resolve prerequisite dependencies.

**Stage Capping & Confidence Metric:** Restricts each semester bucket to the top 3-4 courses based on pathway consensus, scoring confidence as the ratio of shared edges to total unique edges.

## 3. Data Flow & Latency Optimization

Rather than executing heavy graph queries on every client interaction, the architecture separates storage, compute, and read paths:

| Layer | Component | Role in Graph Engine |
|-------|-----------|---------------------|
| Persistence | PostgreSQL | Stores relational student profiles, courses, and array-indexed transcripts with pg_trgm fuzzy matching. |
| Compute | FastAPI + NumPy | Runs background worker jobs that compute similarity vectors, adjacency matrices, and graph clustering. |
| Cache Layer | Redis | Stores pre-computed graph node-link arrays (nodes: [...], links: [...]) keyed by user trajectory and path IDs. |
| Client | Frontend | Renders dynamic Sankey flow diagrams directly from pre-computed node-link payloads with zero client compute. |
