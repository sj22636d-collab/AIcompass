// === 画面遷移の制御 ===
function showView(viewId) {
    document.querySelectorAll('.view-section').forEach(el => el.classList.add('hidden'));
    document.getElementById(viewId).classList.remove('hidden');
}

function goHome() {
    showView('main-menu');
    // 入力や結果をリセット
    document.getElementById('task-input-text').value = '';
    document.getElementById('condition-input-text').value = '';
    document.getElementById('task-result').classList.add('hidden');
    document.getElementById('schedule-result').classList.add('hidden');
}

document.getElementById('btn-show-add-task').addEventListener('click', () => showView('add-task-view'));
document.getElementById('btn-show-schedule').addEventListener('click', () => showView('schedule-view'));

// === API通信: タスク情報の推測 ===
document.getElementById('btn-submit-task').addEventListener('click', async () => {
    const inputText = document.getElementById('task-input-text').value;
    if (!inputText) return alert("タスクを入力してください");

    document.getElementById('loading').classList.remove('hidden');
    
    try {
        const response = await fetch("http://127.0.0.1:8000/api/extract_task", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ user_input: inputText })
        });
        
        const data = await response.json();
        
        // 結果を画面に反映
        document.getElementById('res-title').textContent = data.title;
        document.getElementById('res-deadline').textContent = data.deadline;
        document.getElementById('res-time').textContent = data.estimatedMinutes;
        document.getElementById('res-category').textContent = data.category;
        document.getElementById('res-reason').textContent = data.reason;
        
        document.getElementById('task-result').classList.remove('hidden');
    } catch (error) {
        alert("エラーが発生しました。サーバーが起動しているか確認してください。");
    } finally {
        document.getElementById('loading').classList.add('hidden');
    }
});

// === API通信: 今日のプラン生成 ===
document.getElementById('btn-submit-condition').addEventListener('click', async () => {
    const conditionText = document.getElementById('condition-input-text').value;
    if (!conditionText) return alert("今の状態を入力してください");

    document.getElementById('loading').classList.remove('hidden');
    
    try {
        const response = await fetch("http://127.0.0.1:8000/api/generate_schedule", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ condition_text: conditionText })
        });
        
        const data = await response.json();
        
        // AIメッセージを反映
        document.getElementById('res-ai-message').textContent = data.ai_message;
        
        // タスクリストを描画
        const listEl = document.getElementById('schedule-list');
        listEl.innerHTML = ''; // リセット
        
        data.schedule.forEach(task => {
            const li = document.createElement('li');
            li.innerHTML = `
                <strong>${task.title || task.task_id}</strong>
                <div class="task-reason">💡 ${task.reason}</div>
            `;
            listEl.appendChild(li);
        });
        
        document.getElementById('schedule-result').classList.remove('hidden');
    } catch (error) {
        alert("エラーが発生しました。サーバーが起動しているか確認してください。");
    } finally {
        document.getElementById('loading').classList.add('hidden');
    }
});