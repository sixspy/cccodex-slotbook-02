export const transferViewTemplate = `
  <div v-show="tab===6" class="panel">
    <h2>跨档转移</h2>
    <p class="sk">口径：同事务释源 + 占目标（路径 A）；任一步失败两边同滚，源仍占、目标零增。</p>
    <div>
      <input v-model.number="transferForm.booking_id" type="number" placeholder="正式册 ID" style="width:8rem" />
      <input v-model="transferForm.to_slot_code" placeholder="目标档编码" />
      <input v-model="transferForm.note" placeholder="备注（可选）" />
      <button @click="doTransfer">转移</button>
    </div>
    <p class="err" v-if="actionErr">{{ actionErr }}</p>
    <table>
      <thead><tr><th>ID</th><th>档</th><th>当事人</th><th>席</th><th>态</th></tr></thead>
      <tbody>
        <tr v-for="b in bookings" :key="b.id">
          <td>{{ b.id }}</td><td>{{ b.slot_code }}</td><td>{{ b.party_key }}</td>
          <td>{{ b.seats }}</td><td>{{ b.status }}</td>
        </tr>
      </tbody>
    </table>
  </div>
`;
