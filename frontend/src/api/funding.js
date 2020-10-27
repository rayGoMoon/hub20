import session from './session';

export default {
  createPaymentOrder(token, amount) {
    return session.post('/api/payment/orders', {
      amount: amount,
      token: token.address
    })
  },
  getPaymentOrder(orderId) {
    return session.get(`/api/payment/orders/${orderId}`);
  },
  cancelPaymentOrder(orderId) {
    return session.delete(`/api/payment/orders/${orderId}`);
  },
  scheduleExternalTransfer(token, amount, address, options) {
    let payload = {
      amount: amount,
      token: token.address,
      address: address,
      ...options
    }
    return session.post('/api/transfers', payload)
  }
};
