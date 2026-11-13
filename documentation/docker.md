docker compose -f docker-compose.yml -f docker-compose.test.yml -p test up -d

docker compose -f docker-compose.yml -f docker-compose.test.yml -p test up -d --build


docker compose -f docker-compose.yml -f docker-compose.test.yml -p test down


docker compose -f docker-compose.yml -f docker-compose.test.yml -p test logs -f


docker compose -f docker-compose.yml -f docker-compose.test.yml -p test restart




docker compose -f docker-compose.yml -f docker-compose.test.yml -p test down -v



docker build .




# NEW


docker build -t api:test .

docker stack deploy -c deployment\docker\api\docker-compose.yml --with-registry-auth prod


docker stack rm prod