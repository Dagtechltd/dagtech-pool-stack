# Simple Docker Commands

Run these commands from:

```bash
cd /opt/miner/pool-stack-docker
```

## Stop the stack without destroying logs

This stops the containers but keeps them, so Docker logs are preserved.

```bash
docker compose -f docker-compose-miner.yml stop
```

## Start the stack again

```bash
docker compose -f docker-compose-miner.yml start
```

If the containers do not already exist, use:

```bash
docker compose -f docker-compose-miner.yml up -d
```

## Restart the stack without destroying logs

```bash
docker compose -f docker-compose-miner.yml restart
```

## Check running containers

```bash
docker compose -f docker-compose-miner.yml ps
```

```bash
docker ps
```

## Watch pool payment/share messages

```bash
docker logs -n 10 -f bdagminer-pool-1 2>&1 | grep "💵"
```

## Watch recent pool logs

```bash
docker logs -n 50 -f bdagminer-pool-1
```

## Watch node logs

```bash
docker logs -n 50 -f bdagminer-node-1
```

## Watch dashboard logs

```bash
docker logs -n 50 -f bdagminer-dashboard-1
```

## Watch postgres logs

```bash
docker logs -n 50 -f bdagminer-postgres-1
```

## Important: commands to avoid if you want to keep Docker logs

Do not use these unless you are intentionally removing containers/data:

```bash
docker compose -f docker-compose-miner.yml down
docker compose -f docker-compose-miner.yml rm
docker volume rm ...
docker system prune
```

`down` removes containers, which removes the normal Docker container logs.
