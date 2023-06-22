import logging
import time

import numpy as np
from spare_scores.data_prep import logging_basic_config
import matplotlib.pyplot as plt 

from sklearn.model_selection import train_test_split 
from sklearn.metrics import confusion_matrix, mean_absolute_error, r2_score, mean_squared_error, roc_auc_score, mean_absolute_error
from sklearn.exceptions import ConvergenceWarning
from sklearn.preprocessing import StandardScaler
from sklearn.utils._testing import ignore_warnings

import torch
import torch.nn as nn 
from torch.utils.data import Dataset, DataLoader
import torch.optim as optim

from ray import tune
from ray.air import Checkpoint, session
from ray.tune.schedulers import ASHAScheduler
from functools import partial

device = "cuda" if torch.cuda.is_available() else "cpu"

class MLPDataset(Dataset):
    def __init__(self, X, y):
        self.X = np.array(X, dtype=np.float32)
        self.y = np.array(y, dtype=np.float32)

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

class SimpleMLP(nn.Module):
    def __init__(self, num_features = 147, hidden_size = 256, classification = True, dropout = 0.2, use_bn = False, bn = 'bn'):
        super(SimpleMLP, self).__init__()

        self.num_features   = num_features
        self.hidden_size    = hidden_size 
        self.dropout        = dropout
        self.classification = classification
        self.use_bn         = use_bn

        self.linear1 = nn.Linear(self.num_features, self.hidden_size)
        self.norm1 = nn.InstanceNorm1d(self.hidden_size , eps=1e-15) if bn != 'bn' else nn.BatchNorm1d(self.hidden_size, eps=1e-15)

        self.linear2 = nn.Linear(self.hidden_size,  self.hidden_size//2)
        self.norm2 = nn.InstanceNorm1d(self.hidden_size //2 , eps=1e-15) if bn != 'bn' else nn.BatchNorm1d(self.hidden_size //2, eps=1e-15)

        self.linear3 = nn.Linear(self.hidden_size//2 , 1)

        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(p = 0.2)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        ## first layer
        x = self.linear1(x)
        if self.use_bn:
            x = self.norm1(x)
        x = self.dropout(self.relu(x))

        ## second layer
        x = self.linear2(x)
        if self.use_bn:
            x = self.norm2(x)
        x = self.relu(x)
        x = self.dropout(x)

        ## thrid layer
        x = self.linear3(x)

        if self.classification:
            x = self.sigmoid(x)
        else:
            x = self.relu(x)

        return x.squeeze()

class MLPTorchModel:
    """
    A class for managing MLP models.

    Static attributes:
        predictors (list): List of predictors used for modeling.
        to_predict (str): Target variable for modeling.
        key_var (str): Key variable for modeling.

    Additionally, the class can be initialized with any number of keyword
    arguments. These will be added as attributes to the class.

    Methods:
        train_model(df, **kwargs):
            Trains the model using the provided dataframe.
        
        apply_model(df):
            Applies the trained model on the provided dataframe and returns
            the predictions.
        
        set_parameters(**parameters):
            Updates the model's parameters with the provided values. This also
            changes the model's attributes, while retaining the original ones.
    """
    def __init__(self, predictors, to_predict, key_var, verbose=1,**kwargs):
        logger = logging_basic_config(verbose, content_only=True)
        
        self.predictors = predictors
        self.to_predict = to_predict
        self.key_var = key_var
        self.verbose = verbose

        valid_parameters = ['task', 'gpu', 'cpu', 'bs', 'num_epoches']

        for parameter in kwargs.keys():
            if parameter not in valid_parameters:
                print("Parameter '" + parameter + "' is not accepted for "
                        +"MLPModel. Ignoring...")
                continue
            
            if parameter == 'task':
                if kwargs[parameter] not in ['Classification', 'Regression']:
                    logger.error("Only 'Classification' and 'Regression' "
                                    + "tasks are supported.")
                    raise ValueError("Only 'Classification' and 'Regression' "
                                    + "tasks are supported.")
                else:
                    self.task = kwargs[parameter]
                continue

            if parameter == 'gpu':
                try:
                    self.gpu = int(kwargs[parameter])
                except ValueError:
                    print('Parameter: # of gpu should be integer')

            if parameter == 'cpu':
                try:
                    self.gpu = int(kwargs[parameter])
                except ValueError:
                    print('Parameter: # of gpu should be integer')

            if parameter == 'bs':
                try:
                    self.batch_size = int(kwargs[parameter])
                except ValueError:
                    print('Parameter: # of gpu should be integer')

            if parameter == 'num_epoches':
                try:
                    self.num_epochs = int(kwargs[parameter])
                except ValueError:
                    print('Parameter: # of gpu should be integer')

            self.__dict__.update({parameter: kwargs[parameter]})

        # Set default values for the parameters if not provided

        if 'task' not in kwargs.keys():
            self.task = 'Regression'

        if 'gpu' not in kwargs.keys():
            self.gpu = 1

        if 'cpu' not in kwargs.keys():
            self.cpu = 1

        if 'batch_size' not in kwargs.keys():
            self.batch_size = 128

        if 'num_epochs' not in kwargs.keys():
            self.num_epochs = 100

        if device != 'cuda':
            print('You are not using the GPU! Check your device')

        ################################## MODEL SETTING ##################################################
        self.classification = True if self.task == 'Classification' else False
        self.mdl        = None
        self.scaler     = None
        self.stats      = None
        self.param      = None
        self.train_dl   = None
        self.val_dl     = None
        self.config     = {
                            "hidden_size": tune.choice([128, 256, 512]),
                            "dropout": tune.choice([0.1, 0.2, 0.25, 0.5]),
                            "lr": tune.loguniform(1e-4, 1e-1),
                            'use_bn' : tune.choice(['False', 'True']),
                            'bn' : tune.choice(['in', 'bn'])
                         }
        ################################## MODEL SETTING ##################################################

    def get_all_stats(self, y_hat, y, classification = True):
        """
        Input: 
            y:     ground truth y (1: AD, 0: CN) -> numpy 
            y_hat: predicted y -> numpy, notice y_hat is predicted value [0.2, 0.8, 0.1 ...]

        Output:
            A dictionary contains: Acc, F1, Sensitivity, Specificity, Balanced Acc, Precision, Recall
        """
        y = np.array(y)
        y_hat = np.array(y_hat)
        if classification: 
            auc = roc_auc_score(y, y_hat)

            y_hat = np.where(y_hat >= 0.5, 1 , 0)
            
            tn, fp, fn, tp = confusion_matrix(y, y_hat).ravel()

            acc = (tp + tn) / (fp + fn + tp + tn)
            sensitivity = tp / (tp + fn)
            specificity = tn / (tn + fp)
            balanced_acc = (sensitivity + specificity) / 2
            precision   = tp / (tp + fp)
            recall      = tp / (tp + fn)
            F1          = 2 * (precision * recall ) / (precision + recall)

            dict = {}
            dict['Accuracy']          = acc
            dict['AUC']               = auc
            dict['Sensitivity']       = sensitivity
            dict['Specificity']       = specificity
            dict['Balanced Accuarcy'] = balanced_acc
            dict['Precision']         = precision
            dict['Recall']            = recall
            dict['F1']                = F1
  
        else:
            dict = {}
            mae  = mean_absolute_error(y, y_hat)
            mrse = mean_squared_error(y, y_hat, squared=False)
            r2   = r2_score(y, y_hat)
            dict['MAE']  = mae
            dict['RMSE'] = mrse
            dict['R2']   = r2

        return dict 
    

    def train(self, config):

        evaluation_metric = 'Accuracy' if self.task == 'Classification' else 'MAE'

        model = SimpleMLP(num_features =len(self.predictors), hidden_size = int(config['hidden_size']), classification= self.classification, dropout= config['dropout'], use_bn= config['use_bn'], bn = str(config['bn']))
        optimizer = optim.Adam(model.parameters(), lr = config['lr'])
        loss_fn = nn.BCELoss() if self.classification else nn.L1Loss()

        device = "cpu"
        if torch.cuda.is_available():
            device = "cuda:0"
            if torch.cuda.device_count() > 1:
                model = nn.DataParallel(model)
        model.to(device)
        model.train()

        checkpoint = session.get_checkpoint()

        if checkpoint:
            checkpoint_state = checkpoint.to_dict()
            start_epoch = checkpoint_state["epoch"]
            model.load_state_dict(checkpoint_state["net_state_dict"])
            optimizer.load_state_dict(checkpoint_state["optimizer_state_dict"])
        else:
            start_epoch = 0


        for epoch in range(start_epoch, self.num_epochs):

            step = 0

            for _, (x,y) in enumerate(self.train_dl):
                step += 1
                x = x.to(device)
                y = y.to(device)

                output = model(x)
                optimizer.zero_grad()

                loss = loss_fn(output, y)

                loss.backward()

                optimizer.step()


            val_step = 0
            val_total_metric = 0
            val_total_loss = 0

            with torch.no_grad():
                for _, (x, y) in enumerate(self.val_dl):
                    val_step += 1
                    x = x.to(device)
                    y = y.to(device)
                    output = model(x.float())

                    loss = loss_fn(output, y)
                    val_total_loss += loss.item()
                    metric = self.get_all_stats(output.cpu().data.numpy(), y.cpu().data.numpy() , classification= self.classification)[evaluation_metric]
                    val_total_metric += metric

                val_total_loss = val_total_loss / val_step
                val_total_metric  = val_total_metric / val_step 

            checkpoint_data = {
                "epoch": epoch,
                "net_state_dict": model.state_dict(),
                "optimizer_state_dict": optimizer.state_dict(),
            }
            checkpoint = Checkpoint.from_dict(checkpoint_data)


            session.report(
                {"loss": val_total_loss, "metric": val_total_metric },
                checkpoint=checkpoint,
            )

        print('finish training') 
        return 
    
   
    def set_parameters(self, **parameters):
        if 'linear1.weight' in parameters.keys():
            self.param = parameters
        else:
            self.__dict__.update(parameters)
        
    @ignore_warnings(category= (ConvergenceWarning,UserWarning))
    def fit(self, df, verbose=1, **kwargs):
        logger = logging_basic_config(verbose, content_only=True)
        
        
        # Time the training:
        start_time = time.time()

        logger.info(f'Training the MLP model...')
        
        ############################################ start training model here ####################################
        X = df[self.predictors]
        y = df[self.to_predict].tolist()

        X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42)
        X_train = X_train.reset_index(drop = True)
        X_val = X_val.reset_index(drop = True)

        self.scaler = StandardScaler().fit(X_train)
        X_train = self.scaler.transform(X_train)
        X_val  = self.scaler.transform(X_val)

        train_ds = MLPDataset(X_train, y_train)
        val_ds   = MLPDataset(X_val, y_val)

        self.train_dl = DataLoader(train_ds, batch_size = self.batch_size, shuffle = True)
        self.val_dl   = DataLoader(val_ds, batch_size = self.batch_size, shuffle = True)

        scheduler = ASHAScheduler(
            metric="loss",
            mode="min",
            max_t=100,
            grace_period=1,
            reduction_factor=2,
        )

        result = tune.run(
            partial(self.train),
            resources_per_trial={"cpu": self.cpu, "gpu": self.gpu},
            config=self.config,
            num_samples= 10,
            scheduler=scheduler
        )

        best_trial = result.get_best_trial("loss", "min", "last")
        print(f"Best trial config: {best_trial.config}")
        print(f"Best trial final validation loss: {best_trial.last_result['loss']}")
        print(f"Best trial final validation metric: {best_trial.last_result['metric']}")
        
        self.mdl = SimpleMLP(num_features = len(self.predictors), hidden_size = int(best_trial.config['hidden_size']), classification= self.classification, dropout= best_trial.config['dropout'], use_bn= best_trial.config['use_bn'], bn = str(best_trial.config['bn']))
        best_checkpoint = best_trial.checkpoint.to_air_checkpoint()
        best_checkpoint_data = best_checkpoint.to_dict()
        self.mdl.load_state_dict(best_checkpoint_data["net_state_dict"])
        self.mdl.to(device)
        self.mdl.eval()
        X_total = self.scaler.transform( np.array(X, dtype = np.float32) )
        X_total = torch.tensor(X_total).to(device)
        
        self.y_pred = self.mdl(X_total).cpu().data.numpy()
        self.stats = self.get_all_stats(self.y_pred, y, classification = self.classification)

        self.param =  best_checkpoint_data["net_state_dict"]

        ########################################################################################################### 

        training_time = time.time() - start_time
        self.stats['training_time'] = round(training_time, 4)


        result = {'predicted':self.y_pred, 
                  'model':self.mdl, 
                  'stats':self.stats, 
                  'best_params': self.param,
                  'CV_folds': None,
                  'scaler': self.scaler}
    
        if self.task == 'Regression':
            print('>>MAE = ', self.stats['MAE'])
            print('>>RMSE = ', self.stats['RMSE'])
            print('>>R2 = ', self.stats['R2'])

        else:
            print('>>AUC = ', self.stats['AUC'])
            print('>>Accuracy = ', self.stats['Accuracy'])
            print('>>Sensityvity = ', self.stats['Sensitivity'])
            print('>>Specificity = ', self.stats['Specificity'])
            print('>>Precision = ', self.stats['Precision'])
            print('>>Recall = ', self.stats['Recall'])
            print('>>F1 = ', self.stats['F1'])

        return result 
    
    def predict(self, df):
        
        X = df[self.predictors]
        X = self.scaler.transform(np.array(X, dtype = np.float32))
        X = torch.tensor(X).to(device)

        checkpoint_dict = self.param
        self.mdl.load_state_dict(checkpoint_dict)
        self.mdl.eval()

        y_pred = self.mdl(X).cpu().data.numpy()

        return y_pred if self.task == 'Regression' else np.where(y_pred >= 0.5, 1 , 0)

    def output_stats(self):
        [logging.info(f'>> {key} = {np.mean(value):#.4f} \u00B1 {np.std(value):#.4f}') for key, value in self.stats.items()]
