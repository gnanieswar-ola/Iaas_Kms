FROM python:3.8-slim

RUN apt-get update && apt-get install -y \
    ansible \
    curl \
    git \
    python3-apt \
    sshpass \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app
COPY roles/ /app
COPY privatekey.pem /app

WORKDIR /app

RUN echo "- name: Deploy RKE2\n\
  hosts: k8s_cluster\n\
  become: yes\n\
  roles:\n\
     - role: lablabs.rke2" > /app/rke2.yml

RUN echo "- name: Uninstall RKE2\n\
  hosts: all\n\
  become: yes\n\
  tasks:\n\
    - name: Run uninstall script\n\
      shell: \"/usr/local/bin/rke2-uninstall.sh\"" > /app/uninstall.yml

# Install necessary Python packages including Flask, Flask-CORS, and pymongo
RUN pip install --trusted-host pypi.python.org Flask flask-cors pymongo

COPY app.py /app

EXPOSE 5000
ENV NAME World

CMD ["python", "app.py"]

