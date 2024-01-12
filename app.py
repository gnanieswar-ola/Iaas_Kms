# app.py
from flask import Flask, request, jsonify, abort
from flask_cors import CORS
from pymongo import MongoClient
import subprocess
import ipaddress
from threading import Thread
import uuid
import json

app = Flask(__name__)
CORS(app)

# Global variables
cluster_creation_status = {'status': 'pending', 'message': 'Cluster creation in progress'}
upgrade_status = {'status': 'pending', 'message': 'Cluster upgrade in progress'}
latest_valid_request_id = None
clusters_info = {}
ansible_playbook_response = None

# MongoDB configuration
client = MongoClient('mongodb://172.31.89.139:27017/')
db = client['cluster_db']
clusters_collection = db['clusters']

def get_cluster_info(cluster_name):
    """
    Retrieve cluster information from MongoDB based on the cluster name.

    Parameters:
    - cluster_name (str): Name of the cluster.

    Returns:
    - dict: Cluster information or None if not found.
    """
    return clusters_collection.find_one({'cluster_name': cluster_name})

def save_cluster_info(cluster_name, request_id):
    """
    Save cluster information to MongoDB.

    Parameters:
    - cluster_name (str): Name of the cluster.
    - request_id (str): Unique identifier for the cluster creation request.
    """
    clusters_collection.insert_one({'cluster_name': cluster_name, 'request_id': request_id})


def generate_inventory(master_ips, worker_ips):
    inventory_content = "[masters]\n"
    inventory_content += "\n".join([f"master-{i} ansible_host={ip} rke2_type=server" for i, ip in enumerate(master_ips, 1)])

    inventory_content += "\n\n[workers]\n"
    inventory_content += "\n".join([f"worker-{i} ansible_host={ip} rke2_type=agent" for i, ip in enumerate(worker_ips, 1)])

    inventory_content += "\n\n[k8s_cluster:children]\n"
    inventory_content += "masters\nworkers"

    return inventory_content

def run_ansible_playbook(ansible_command):
    global cluster_creation_status, upgrade_status, ansible_playbook_response
    try:
        output = subprocess.check_output(ansible_command, stderr=subprocess.STDOUT, text=True)
        cluster_creation_status = {'status': 'success', 'message': 'Cluster creation successful'}
        upgrade_status = {'status': 'success', 'message': 'Cluster upgrade successful'}
        ansible_playbook_response = output
    except subprocess.CalledProcessError as e:
        error_message = f'Cluster creation or upgrade failed: {str(e)}'
        if e.output is not None:
            error_message += f'\n{e.output}'
        cluster_creation_status = {'status': 'internal error', 'message': error_message}
        upgrade_status = {'status': 'internal error', 'message': error_message}
        ansible_playbook_response = None

def start_ansible_playbook(ansible_command):
    Thread(target=run_ansible_playbook, args=(ansible_command,)).start()

def validate_ip_addresses(ips):
    try:
        return [str(ipaddress.ip_address(ip)) for ip in ips]
    except ValueError as e:
        abort(400, jsonify({'status': 'error', 'message': f'Invalid IP address: {str(e)}'}))

def create_dynamic_inventory(master_ips, worker_ips):
    try:
        inventory_content = generate_inventory(master_ips, worker_ips)
        with open('/tmp/dynamic_inventory.ini', 'w') as inventory_file:
            inventory_file.write(inventory_content)
    except Exception as e:
        abort(500, jsonify({'status': 'error', 'message': f'Error creating dynamic inventory: {str(e)}'}))

def build_ansible_command(rke2_version, upgrade_required=False):
    try:
        playbook_path = '/app/rke2.yml'
        ansible_command = [
            'ansible-playbook',
            '-i', '/tmp/dynamic_inventory.ini',
            playbook_path,
            '--user', 'ubuntu',
            '--private-key', 'privatekey.pem',
            '--ssh-common-args', '-o StrictHostKeyChecking=no'
        ]

        if rke2_version:
            ansible_command.extend(['-e', f'rke2_version={rke2_version}'])
        ansible_command.extend(['-e', f'rke2_drain_node_during_upgrade={upgrade_required}'])

        return ansible_command
    except Exception as e:
        abort(500, jsonify({'status': 'error', 'message': f'Error building Ansible command: {str(e)}'}))

# Modify the create_cluster function
@app.route('/api/cluster/create', methods=['POST'])
def create_cluster():
    global cluster_creation_status, latest_valid_request_id

    rke2_version = request.json.get('rke2_k8s_version', None)
    master_ips = request.json.get('master_ips', None)
    worker_ips = request.json.get('worker_ips', [])
    cluster_name = request.json.get('cluster_name', None)

    if not rke2_version or not master_ips or not cluster_name:
        missing_params = ', '.join(param for param, value in {'rke2_k8s_version': rke2_version, 'master_ips': master_ips, 'cluster_name': cluster_name}.items() if not value)
        error_message = f'Both rke2_k8s_version, master_ips, and cluster_name are required attributes. Please pass the missing parameter(s) in the JSON payload: {missing_params}'
        return jsonify({'status': 'error', 'message': error_message}), 400

    # Check if cluster with the same master IPs and worker IPs already exists in MongoDB
    existing_cluster = clusters_collection.find_one({'master_ips': master_ips, 'worker_ips': worker_ips})
    if existing_cluster:
        return jsonify({'status': 'error', 'message': f'Cluster with the same master_ips and worker_ips already exists.'}), 400

    # Check if cluster with the same name already exists in MongoDB
    existing_cluster = clusters_collection.find_one({'cluster_name': cluster_name})
    if existing_cluster:
        return jsonify({'status': 'error', 'message': f'Cluster with name "{cluster_name}" already exists.'}), 400

    request_id = str(uuid.uuid4())
    cluster_creation_status = {'status': 'pending', 'message': 'Cluster creation in progress', 'request_id': request_id, 'cluster_name': cluster_name}
    latest_valid_request_id = request_id

    master_ips = validate_ip_addresses(master_ips)
    worker_ips = validate_ip_addresses(worker_ips)

    create_dynamic_inventory(master_ips, worker_ips)
    ansible_command = build_ansible_command(rke2_version)
    start_ansible_playbook(ansible_command)

    # Store cluster information in MongoDB
    clusters_collection.insert_one({
        'cluster_name': cluster_name,
        'request_id': request_id,
        'master_ips': master_ips,
        'worker_ips': worker_ips
    })

    return jsonify({'status': 'success', 'message': 'Cluster creation request sent successfully', 'request_id': request_id, 'cluster_name': cluster_name})


@app.route('/api/cluster/upgrade', methods=['POST'])
def upgrade_cluster():
    global upgrade_status, latest_valid_request_id

    rke2_version = request.json.get('rke2_k8s_version', None)
    master_ips = request.json.get('master_ips', None)
    worker_ips = request.json.get('worker_ips', [])
    upgrade_required = request.json.get('upgrade_required', False)
    cluster_name = request.json.get('cluster_name', None)

    if not rke2_version or not master_ips or not cluster_name:
        missing_params = ', '.join(param for param, value in {'rke2_k8s_version': rke2_version, 'master_ips': master_ips, 'cluster_name': cluster_name}.items() if not value)
        error_message = f'Both rke2_k8s_version, master_ips, and cluster_name are required attributes. Please pass the missing parameter(s) in the JSON payload: {missing_params}'
        return jsonify({'status': 'error', 'message': error_message}), 400

    request_id = str(uuid.uuid4())
    upgrade_status = {'status': 'pending', 'message': 'Cluster upgrade in progress', 'request_id': request_id}
    latest_valid_request_id = request_id

    master_ips = validate_ip_addresses(master_ips)
    worker_ips = validate_ip_addresses(worker_ips)

    create_dynamic_inventory(master_ips, worker_ips)
    ansible_command = build_ansible_command(rke2_version, upgrade_required)
    start_ansible_playbook(ansible_command)

    return jsonify({'status': 'success', 'message': 'Cluster upgrade request sent successfully', 'request_id': request_id, 'cluster_name': cluster_name})

@app.route('/api/cluster/status', methods=['GET'])
def get_cluster_status():
    global cluster_creation_status, latest_valid_request_id, ansible_playbook_response

    cluster_name = request.json.get('cluster_name', None)

    if not cluster_name:
        return jsonify({'status': 'error', 'message': 'Cluster name not provided in the JSON payload'}), 400

    # Retrieve cluster information from MongoDB
    cluster_info = clusters_collection.find_one({'cluster_name': cluster_name})

    if cluster_info:
        status_without_request_id = {
            'message': cluster_creation_status['message'],
            'status': cluster_creation_status['status'],
            'ansible_playbook_response': None
        }

        if cluster_creation_status['status'] == 'success':
            http_status_code = 200
            if ansible_playbook_response:
                nodes_summary_start = ansible_playbook_response.find('"nodes_summary.stdout_lines": [')
                nodes_summary_end = ansible_playbook_response.find('PLAY RECAP', nodes_summary_start)
                nodes_summary = ansible_playbook_response[nodes_summary_start:nodes_summary_end]

                nodes_summary_cleaned = (
                    nodes_summary
                    .replace('\\"', '"')
                    .replace('\\n', '\n')
                    .replace('\n', '')
                    .replace('    ', '')
                )

                nodes_summary_list = list({nodes_summary_cleaned.strip()})

                status_without_request_id['ansible_playbook_response'] = json.dumps(
                    nodes_summary_list,
                    indent=4
                )

        elif cluster_creation_status['status'] == 'pending':
            http_status_code = 202
        elif cluster_creation_status['status'] == 'internal error':
            http_status_code = 500
        else:
            http_status_code = 500

        return jsonify(status_without_request_id), http_status_code
    else:
        return jsonify({'status': 'error', 'message': f'Cluster with name "{cluster_name}" not found'}), 404


@app.route('/api/upgrade/status', methods=['GET'])
def get_upgrade_status():
    global upgrade_status, latest_valid_request_id, ansible_playbook_response

    status_without_request_id = {
        'message': upgrade_status['message'],
        'status': upgrade_status['status'],
        'ansible_playbook_response': None  # Initialize to None
    }

    if upgrade_status['status'] == 'success':
        http_status_code = 200
        if ansible_playbook_response:
            nodes_summary_start = ansible_playbook_response.find('"nodes_summary.stdout_lines": [')
            nodes_summary_end = ansible_playbook_response.find('PLAY RECAP', nodes_summary_start)
            nodes_summary = ansible_playbook_response[nodes_summary_start:nodes_summary_end]

            nodes_summary_cleaned = (
                nodes_summary
                .replace('\\"', '"')
                .replace('\\n', '\n')
                .replace('\n', '')
                .replace('    ', '')
            )

            nodes_summary_list = list({nodes_summary_cleaned.strip()})

            status_without_request_id['ansible_playbook_response'] = json.dumps(
                nodes_summary_list,
                indent=4
            )

    elif upgrade_status['status'] == 'pending':
        http_status_code = 202
    elif upgrade_status['status'] == 'internal error':
        http_status_code = 500
    else:
        http_status_code = 500

    return jsonify(status_without_request_id), http_status_code

@app.route('/api/cluster/delete', methods=['DELETE'])
def delete_cluster():
    global clusters_info, delete_status

    cluster_name = request.json.get('cluster_name', None)
    rke2_version = request.json.get('rke2_k8s_version', None)
    master_ips = request.json.get('master_ips', [])
    worker_ips = request.json.get('worker_ips', [])

    if not cluster_name or not rke2_version or not master_ips or not worker_ips:
        missing_params = ', '.join(param for param, value in {'cluster_name': cluster_name, 'rke2_k8s_version': rke2_version, 'master_ips': master_ips, 'worker_ips': worker_ips}.items() if not value)
        error_message = f'cluster_name, rke2_k8s_version, master_ips, and worker_ips are required attributes. Please pass the missing parameter(s) in the JSON payload: {missing_params}'
        return jsonify({'status': 'error', 'message': error_message}), 400

    if cluster_name not in clusters_info:
        return jsonify({'status': 'error', 'message': f'Cluster with name "{cluster_name}" not found'}), 404

    master_ips = validate_ip_addresses(master_ips)
    worker_ips = validate_ip_addresses(worker_ips)

    create_dynamic_inventory(master_ips, worker_ips)

    playbook_path = '/home/ubuntu/uninstall.yml'  # Update with the correct path to uninstall.yml
    ansible_command = [
        'ansible-playbook',
        '-i', '/tmp/dynamic_inventory.ini',
        playbook_path,
        '--user', 'ubuntu',
        '--private-key', 'privatekey.pem',
        '--ssh-common-args', '-o StrictHostKeyChecking=no'
    ]

    if rke2_version:
        ansible_command.extend(['-e', f'rke2_version={rke2_version}'])

    start_ansible_playbook(ansible_command)

    request_id = str(uuid.uuid4())
    delete_status = {'status': 'pending', 'message': 'Cluster deletion in progress', 'request_id': request_id, 'cluster_name': cluster_name}

    del clusters_info[cluster_name]

    return jsonify({'status': 'success', 'message': f'Delete cluster request sent successfully for cluster "{cluster_name}"', 'request_id': request_id, 'cluster_name': cluster_name})

@app.route('/api/cluster/list', methods=['GET'])
def get_cluster_list():
    global clusters_info
    return jsonify({'clusters': list(clusters_info.values())})

@app.errorhandler(400)
def bad_request(error):
    return jsonify({'status': 'error', 'message': 'Bad request'}), 400

@app.errorhandler(401)
def unauthorized(error):
    return jsonify({'status': 'error', 'message': 'Unauthorized'}), 401

@app.errorhandler(404)
def not_found(error):
    return jsonify({'status': 'error', 'message': 'Not found'}), 404

@app.errorhandler(500)
def internal_server_error(error):
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', use_reloader=False)
