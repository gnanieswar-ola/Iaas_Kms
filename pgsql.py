from flask import Flask, request, jsonify, abort
from flask_cors import CORS
import psycopg2
import subprocess
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

# Establish connection to your PostgreSQL database
conn = psycopg2.connect(
    dbname="admin",
    user="admin",
    password="admin",
    host="localhost",
    port="5432"
)

def save_cluster_info(cluster_name, request_id, master_ips, worker_ips):
    try:
        with psycopg2.connect(
            dbname="admin",
            user="admin",
            password="admin",
            host="localhost",
            port="5432"
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO clusters (cluster_name, request_id, master_ips, worker_ips) VALUES (%s, %s, %s, %s)",
                    (cluster_name, request_id, master_ips, worker_ips)
                )
                conn.commit()
    except psycopg2.Error as e:
        print(f"Error inserting data: {e}")
        # Handle the error accordingly

def get_cluster_info(cluster_name):
    try:
        with psycopg2.connect(
            dbname="admin",
            user="admin",
            password="admin",
            host="localhost",
            port="5432"
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM clusters WHERE cluster_name = %s",
                    (cluster_name,)
                )
                result = cursor.fetchone()
                return result
    except psycopg2.Error as e:
        print(f"Error retrieving data: {e}")
        # Handle the error accordingly
        return None

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
        print(output)
    except subprocess.CalledProcessError as e:
        error_message = f'Cluster creation or upgrade failed: {str(e)}'
        if e.output is not None:
            error_message += f'\n{e.output}'
        cluster_creation_status = {'status': 'internal error', 'message': error_message}
        upgrade_status = {'status': 'internal error', 'message': error_message}
        ansible_playbook_response = None
        print(error_message) 

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
        playbook_path = '/home/ubuntu/new_kms/kms/rke2.yml'
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

def get_existing_clusters_with_same_ips(master_ips, worker_ips):
    try:
        with psycopg2.connect(
            dbname="admin",
            user="admin",
            password="admin",
            host="localhost",
            port="5432"
        ) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT cluster_name, master_ips, worker_ips FROM clusters WHERE master_ips = %s OR worker_ips = %s",
                    (master_ips, worker_ips)
                )
                results = cursor.fetchall()
                return results
    except psycopg2.Error as e:
        print(f"Error retrieving data: {e}")
        return []

def cluster_exists(cluster_name, master_ips, worker_ips):
    existing_clusters = get_existing_clusters_with_same_ips(master_ips, worker_ips)
    for cluster in existing_clusters:
        if cluster[0] != cluster_name:
            return True
    return False

# API Endpoint for creating clusters
@app.route('/api/cluster/create', methods=['POST'])
def create_cluster():
    rke2_version = request.json.get('rke2_k8s_version', None)
    master_ips = request.json.get('master_ips', None)
    worker_ips = request.json.get('worker_ips', [])
    cluster_name = request.json.get('cluster_name', None)

    if not rke2_version or not master_ips or not cluster_name:
        missing_params = ', '.join(param for param, value in {'rke2_k8s_version': rke2_version, 'master_ips': master_ips, 'cluster_name': cluster_name}.items() if not value)
        error_message = f'Both rke2_k8s_version, master_ips, and cluster_name are required attributes. Please pass the missing parameter(s) in the JSON payload: {missing_params}'
        return jsonify({'status': 'error', 'message': error_message}), 400

    # Check if the cluster with the same name and IPs already exists
    if cluster_exists(cluster_name, master_ips, worker_ips):
        return jsonify({'status': 'error', 'message': 'Cluster with the same name and IPs already exists.'}), 400

    try:
        request_id = str(uuid.uuid4())
        # Pass master_ips and worker_ips to save_cluster_info() function
        save_cluster_info(cluster_name, request_id, master_ips, worker_ips)

        # Create dynamic inventory
        create_dynamic_inventory(master_ips, worker_ips)

        # Build and start Ansible playbook
        ansible_command = build_ansible_command(rke2_version)
        start_ansible_playbook(ansible_command)

        return jsonify({'status': 'success', 'message': 'Cluster creation request sent successfully', 'request_id': request_id, 'cluster_name': cluster_name})

    except psycopg2.Error as e:
        conn.rollback()  # Rollback the transaction
        error_message = f"Error creating cluster: {str(e)}"
        return jsonify({'status': 'error', 'message': error_message}), 500

    finally:
        conn.close()
        print("PostgreSQL connection is closed")

@app.route('/api/cluster/status', methods=['POST'])
def get_cluster_status():
    global cluster_creation_status, ansible_playbook_response

    payload = request.json
    cluster_name = payload.get('cluster_name')

    if not cluster_name:
        return jsonify({'status': 'error', 'message': 'Cluster name not provided in the JSON payload'}), 400

    # Retrieve cluster information from your data source (replace with your logic)
    # For example, assume cluster_creation_status and ansible_playbook_response are globally updated elsewhere in the application

    status_without_request_id = {
        'message': cluster_creation_status['message'],
        'status': cluster_creation_status['status'],
        'ansible_playbook_response': None
    }

    if cluster_creation_status['status'] == 'success':
        http_status_code = 200
        if ansible_playbook_response:
            # Logic to extract and format Ansible playbook response if available
            # Assuming the response should only be displayed upon a successful cluster creation
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
    else:
        # For statuses other than 'success', ensure the Ansible playbook response is not displayed
        http_status_code = 202 if cluster_creation_status['status'] == 'pending' else 500

    return jsonify(status_without_request_id), http_status_code


# Close the connection when done
@app.teardown_appcontext
def close_connection(exception):
    conn.close()
    print("PostgreSQL connection is closed")


if __name__ == '__main__':
    # Run the Flask app
    app.run(debug=True, host='0.0.0.0', port=5000)
