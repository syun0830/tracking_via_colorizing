import tensorflow as tf

def stratified_sample(probs, n):
    N = tf.shape(probs)[0:1]
    c = tf.cumsum(probs)
    c = c/c[-1]
    borders = tf.linspace(0.,1.,n+1)
    right = borders[1:]

    c = tf.expand_dims(c, 0)
    right = tf.expand_dims(right, 1)
    greater_mask = tf.cast(tf.greater(c, right), tf.int32)
    _cum_num = tf.reduce_sum(greater_mask, 1)
    cum_num = tf.concat([N,_cum_num[:-1]],0)
    num = cum_num - _cum_num
    unif = tf.contrib.distributions.Uniform(low=0., high=tf.cast(num, tf.float32))
    local_inds = tf.cast(unif.sample(), tf.int32)
    begin = N - cum_num
    return local_inds + begin

class PrioritizedHistory:
    def __init__(self, name_to_shape_dtype,
                 capacity = 100000,
                 device = '/gpu:0',
                 variable_collections = ['history'],
                 scope = 'history',
                 print_messages = False):
        variables = []
        self._capacity = capacity
        self._device = device
        self._scope = scope
        self._print_messages = print_messages

        if not isinstance(name_to_shape_dtype, dict):
            name_to_shape_dtype = {'__singleton__': name_to_shape_dtype}
        
        with tf.device(self._device), tf.name_scope(self._scope):
            self._histories = {}
            with tf.name_scope('data'):
                for name, (shape, dtype) in name_to_shape_dtype.iteritems():
                    self._histories[name] = tf.Variable(tf.zeros([capacity]+list(shape), dtype=dtype),
                                                        trainable = False,
                                                        collections = variable_collections,
                                                        name = name)
                    variables.append(self._histories[name])
        
            self._weights = tf.Variable(tf.zeros([capacity], dtype=tf.float32),
                                        trainable = False,
                                        collections = variable_collections,
                                        name = 'weights')
            variables.append(self._weights)

            self._inds = tf.Variable(tf.range(capacity),
                                     trainable = False,
                                     collections = variable_collections,
                                     name = 'indices')
            variables.append(self._inds)
            
            self._size = tf.Variable(tf.constant(0, dtype=tf.int32),
                                     trainable = False,
                                     collections = variable_collections,
                                     name = 'size')
            variables.append(self._size)
        
            self.saver = tf.train.Saver(var_list=variables)
            self.initializer = tf.group(map(lambda v: v.initializer, variables))

    def append(self, name_to_value, weight):
        if not isinstance(name_to_value, dict):
            name_to_value = {'__singleton__': name_to_value}
        
        with tf.device(self._device), tf.name_scope(self._scope):
            weight = tf.convert_to_tensor(weight)
            name_to_value = {name: tf.convert_to_tensor(value) for name, value in name_to_value.iteritems()}
            inds = tf.where(tf.less(self._weights, weight))
            accepted = tf.greater(tf.shape(inds)[0], 0)
            def insert():
                ind = inds[0,0]
                ind_buf = self._inds[-1]
                ops = []
                for name, value in name_to_value.iteritems():
                    ops.append(self._histories[name][ind_buf].assign(value))
                with tf.control_dependencies(ops):
                    ops = [self._weights[(ind+1):].assign(self._weights[ind:-1]),
                           self._inds[(ind+1):].assign(self._inds[ind:-1])]
                with tf.control_dependencies(ops):
                    ops = [self._weights[ind].assign(weight),
                           self._inds[ind].assign(ind_buf),
                           self._size.assign(tf.reduce_min([self._size+1, self._capacity]))]
                with tf.control_dependencies(ops):
                    ind = tf.cast(ind, tf.int32)
                    if self._print_messages:
                        ind = tf.Print(ind, [ind], message='Entry was inserted at: ')
                        ind = tf.Print(ind, [ind_buf], message='Replaced address: ')
                    return ind
            if self._print_messages:
                return tf.cond(accepted, insert, lambda: tf.Print(-1, [], message='Entry was rejected'))
            else:
                return tf.cond(accepted, insert, lambda: -1)

    def update_weight(self, ind, weight):
        with tf.device(self._device), tf.name_scope(self._scope):
            ind = tf.convert_to_tensor(ind)
            if self._print_messages:
                ind = tf.Print(ind, [ind], message='Updated entry: ')
            old_weight = self._weights[ind]
            ind_buf = self._inds[ind]
            weight = tf.convert_to_tensor(weight)
            def first_less():
                inds = tf.where(tf.less(self._weights, weight))
                return tf.cond(tf.greater(tf.shape(inds)[0], 0),
                               lambda: tf.cast(inds[0,0], tf.int32),
                               lambda: tf.constant(self._capacity-1, dtype=tf.int32))
            def last_greater():
                inds = tf.where(tf.greater(self._weights, weight))
                return tf.cond(tf.greater(tf.shape(inds)[0], 0),
                               lambda: tf.cast(inds[-1,0], tf.int32),
                               lambda: tf.constant(self._capacity-1, dtype=tf.int32))
            new_ind = tf.cond(tf.greater(weight, old_weight), first_less, last_greater)
            if self._print_messages:
                new_ind = tf.Print(new_ind, [new_ind], message='Moved to: ')
            def up():
                ops = [self._weights[ind:new_ind].assign(self._weights[(ind+1):(new_ind+1)]),
                       self._inds[ind:new_ind].assign(self._inds[(ind+1):(new_ind+1)])]
                return tf.group(ops)
            def down():
                ops = [self._weights[(new_ind+1):(ind+1)].assign(self._weights[new_ind:ind]),
                       self._inds[(new_ind+1):(ind+1)].assign(self._inds[new_ind:ind]),]
                return tf.group(ops)
            with tf.control_dependencies([ind_buf]):
                shift = tf.cond(tf.greater(new_ind, ind), up, down)
            with tf.control_dependencies([shift]):
                ops = [self._weights[new_ind].assign(weight),
                       self._inds[new_ind].assign(ind_buf)]
                with tf.control_dependencies(ops):
                    return tf.identity(new_ind)
    
    def update_weights(self, inds, weights):
        with tf.device(self._device), tf.name_scope(self._scope):
            inds = tf.convert_to_tensor(inds)
            if self._print_messages:
                inds = tf.Print(inds, [inds], message='Updated entries: ')
            weights = tf.convert_to_tensor(weights)
            
            updated = tf.scatter_nd_update(self._weights, tf.expand_dims(inds, -1), weights)
            sorted_inds = tf.contrib.framework.argsort(updated,
                                                       direction='DESCENDING',
                                                       stable=True)
            ops = [self._weights.assign(tf.gather(updated, sorted_inds)),
                   self._inds.assign(tf.gather(self._inds, sorted_inds))]
            return tf.group(ops)
    
    def sample(self, size):
        with tf.device(self._device), tf.name_scope(self._scope):
            inds = stratified_sample(self._weights[:self._size], size)
            if self._print_messages:
                inds = tf.Print(inds, [inds], message='Sampled entries: ')
            _inds = tf.gather(self._inds, inds)
            if self._print_messages:
                _inds = tf.Print(_inds, [_inds], message='Sampled addresses: ')
            name_to_value = {name: tf.gather(hist, _inds) for name, hist in self._histories.iteritems()}
            if set(name_to_value.keys()) == set(['__singleton__']):
                name_to_value = name_to_value['__singleton__']
            return inds, name_to_value

if __name__=='__main__':
    sess = tf.InteractiveSession()
    history = PrioritizedHistory(([1], tf.int32),
                                 device = '/cpu:0',
                                 capacity = 5,
                                 print_messages = True)
    def print_vars():
        print('inds:    ', history._inds.eval())
        print('values:  ', history._histories['__singleton__'].eval()[:,0])
        print('weights: ', history._weights.eval())
    sess.run(history.initializer)
    print_vars()
    history.append([1], 1.).eval()
    print_vars()
    history.append([2], 2.).eval()
    print_vars()
    history.append([3], 3.).eval()
    print_vars()
    history.update_weight(1, 4.).eval()
    print_vars()
    history.append([5], 5.).eval()
    print_vars()
    history.append([6], 6.).eval()
    print_vars()
    history.append([25], 2.5).eval()
    print_vars()
    history.update_weights([1], [3.9]).run()
    print_vars()
