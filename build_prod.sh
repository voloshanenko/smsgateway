#! /bin/bash
source env.prod
docker build -t smsgateway .
docker tag smsgateway ${DOCKER_REGISTRY}/smsgateway:latest
docker push ${DOCKER_REGISTRY}/smsgateway:latest
python deploy/deploy.py
