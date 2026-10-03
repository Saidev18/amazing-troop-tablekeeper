# Amazing Troop — Tablekeeper

Submission for the WeAreDevelopers AI Dark Factory challenge using the Tablekeeper track.

## Overview

Tablekeeper is a restaurant reservation system developed progressively across four stages.

The implementation supports restaurant availability, table combinations, authenticated reservations, reservation lookup and cancellation, recurring reservations, history and policy handling, and Stage 4 seating replanning capabilities.

## Stages

### Stage 1
Core reservation API and concurrency-safe booking behavior.

### Stage 2
Customer-facing web interface with:
- Sign up and login
- Restaurant/date/guest search
- Availability display
- Reservation creation
- Reservation lookup
- Reservation cancellation
- Responsive UI

### Stage 3
Extended reservation functionality including policies, history, recurrence, and lifecycle behavior.

### Stage 4
Advanced seating-management functionality including table closures, seating replanning, revisions, recurring-series amendments, and atomic application of replans.

## Running a Stage

Each stage contains its own Dockerfile and RUN.md.

For example, Stage 4 can be run with:

```bash
cd stage-4
docker build -t tablekeeper-stage-4 .
docker run --rm -e PORT=8080 -p 8080:8080 tablekeeper-stage-4
